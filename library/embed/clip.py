"""
CLIP embedder — the backend that makes *text* queries work.

Two implementations are tried in order:

1. ``open_clip_torch`` — the LAION models, best quality/size trade-off and the
   same family the Ultralytics similarity-search guide uses.
2. ``transformers`` ``CLIPModel`` — a common fallback since many environments
   already have it for other reasons.

Both are imported lazily so that a deployment without torch still starts.
"""
from __future__ import annotations

import logging
import threading

import numpy as np
from PIL import Image

from library import settings
from library.embed.base import Embedder, l2_normalise


class ClipUnavailable(RuntimeError):
    """
    Raised when no CLIP backend could be brought up.

    ``packages_missing`` separates the two cases the UI has to word differently:
    the libraries are not installed at all, versus they are installed but the
    weights could not be loaded (offline host, proxy, gated repo).
    """

    def __init__(self, message: str, *, packages_missing: bool) -> None:
        super().__init__(message)
        self.packages_missing = packages_missing


# Values that explicitly select an untrained model, for tests that need to run
# the whole pipeline without downloading 600 MB of weights.
_UNTRAINED_TAGS = {"random", "none"}


def _resolve_pretrained(tag: str) -> str | None:
    """
    Map ``LIB_CLIP_PRETRAINED`` to open_clip's ``pretrained`` argument.

    open_clip treats an empty value as "initialise randomly" and only logs a
    warning, which is a nasty way to lose a day: every embedding comes out
    meaningless but nothing fails.  An empty tag is therefore an error here, and
    untrained models have to be asked for by name.
    """
    tag = (tag or "").strip()
    if not tag:
        raise ValueError(
            "LIB_CLIP_PRETRAINED is empty. Set it to a pretrained tag such as "
            "'laion2b_s34b_b79k', or to 'random' if you deliberately want an "
            "untrained model (tests only — the embeddings are meaningless)."
        )
    return None if tag.lower() in _UNTRAINED_TAGS else tag


def _pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


class ClipEmbedder(Embedder):
    supports_text = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._impl = None
        self.device = _pick_device(settings.CLIP_DEVICE)
        self.name = "clip"
        self.dim = 0
        self.untrained = False
        self._load()

    # ── loading ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        errors: list[str] = []
        packages_missing = True
        for loader in (self._load_open_clip, self._load_transformers):
            try:
                loader()
                return
            except ImportError as exc:
                errors.append(f"{loader.__name__}: {exc}")
            except Exception as exc:                      # noqa: BLE001 - reported below
                # Got past the import, so the package is there and something
                # else (usually the weight download) failed.
                packages_missing = False
                errors.append(f"{loader.__name__}: {exc}")

        if packages_missing:
            raise ClipUnavailable(
                "No CLIP backend available. Install one of:\n"
                "  pip install open_clip_torch torch\n"
                "  pip install transformers torch\n"
                "Tried -> " + " | ".join(errors),
                packages_missing=True,
            )
        # The caller words the "installed but broken" half of this; keep the
        # message to the underlying reason so the two do not stack up.
        raise ClipUnavailable(" | ".join(errors), packages_missing=False)

    def _load_open_clip(self) -> None:
        import open_clip  # noqa: PLC0415
        import torch  # noqa: PLC0415

        pretrained = _resolve_pretrained(settings.CLIP_PRETRAIN)
        model, _, preprocess = open_clip.create_model_and_transforms(
            settings.CLIP_MODEL, pretrained=pretrained, device=self.device
        )
        model.eval()
        self._impl = "open_clip"
        self._torch = torch
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = open_clip.get_tokenizer(settings.CLIP_MODEL)
        self.untrained = pretrained is None
        # The tag is part of the name, and the name is the vector store's
        # identity: changing weights therefore invalidates the stored vectors
        # automatically instead of silently mixing two embedding spaces.
        tag = "random" if self.untrained else settings.CLIP_PRETRAIN
        self.name = f"open_clip/{settings.CLIP_MODEL}/{tag}"
        if self.untrained:
            logging.getLogger(__name__).warning(
                "CLIP is running with RANDOMLY INITIALISED weights "
                "(LIB_CLIP_PRETRAINED=%s). Similarity and text search will be "
                "meaningless. This mode exists to exercise the pipeline in tests.",
                settings.CLIP_PRETRAIN,
            )
        with torch.no_grad():
            probe = model.encode_image(preprocess(Image.new("RGB", (32, 32))).unsqueeze(0).to(self.device))
        self.dim = int(probe.shape[-1])

    def _load_transformers(self) -> None:
        import torch  # noqa: PLC0415
        from transformers import CLIPModel, CLIPProcessor  # noqa: PLC0415

        model = CLIPModel.from_pretrained(settings.CLIP_HF_MODEL).to(self.device).eval()
        self._impl = "transformers"
        self._torch = torch
        self._model = model
        self._processor = CLIPProcessor.from_pretrained(settings.CLIP_HF_MODEL)
        self.name = f"hf/{settings.CLIP_HF_MODEL}"
        self.dim = int(model.config.projection_dim)

    # ── inference ─────────────────────────────────────────────────────────────

    def embed_images(self, images: list[Image.Image]) -> np.ndarray:
        if not images:
            return np.zeros((0, self.dim), dtype=np.float32)
        torch = self._torch
        with self._lock, torch.no_grad():
            if self._impl == "open_clip":
                batch = torch.stack([self._preprocess(im.convert("RGB")) for im in images]).to(self.device)
                feats = self._model.encode_image(batch)
            else:
                inputs = self._processor(images=[im.convert("RGB") for im in images], return_tensors="pt")
                inputs = {k: v.to(self.device) for k, v in inputs.items()}
                feats = self._model.get_image_features(**inputs)
        return l2_normalise(feats.float().cpu().numpy())

    def embed_text(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        torch = self._torch
        with self._lock, torch.no_grad():
            if self._impl == "open_clip":
                feats = self._model.encode_text(self._tokenizer(texts).to(self.device))
            else:
                inputs = self._processor(text=texts, return_tensors="pt", padding=True, truncation=True)
                inputs = {k: v.to(self.device) for k, v in inputs.items()}
                feats = self._model.get_text_features(**inputs)
        return l2_normalise(feats.float().cpu().numpy())
