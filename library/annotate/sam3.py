"""
SAM 3 concept annotator.

SAM 3 does *promptable concept segmentation*: give it noun phrases and it finds
and segments every instance of each concept in the image — "school of fish",
"bleached coral", "diver with camera".  That is exactly the search primitive a
survey library wants, because the vocabulary is not fixed by a checkpoint's
class list the way YOLO's is.

Requirements:

* ``pip install -U ultralytics``  (SAM 3 landed in 8.3.237)
* ``sam3.pt`` — the weights are access-gated on Hugging Face and are **not**
  auto-downloaded.  Request access, download the file, and either place it in
  the working directory or point ``LIB_SAM3_MODEL`` at its full path.

Until those weights exist this annotator reports itself unavailable with that
message rather than failing an indexing run.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from PIL import Image

from library import modelcache, settings
from library.annotate.base import Annotator, xyxy_to_norm
from library.models import AnnotatorStatus, Detection

_WEIGHTS_HELP = (
    "SAM 3 weights not found. Request access at "
    "https://huggingface.co/facebook/sam3, download sam3.pt, then set "
    "LIB_SAM3_MODEL to its path."
)


class Sam3Annotator(Annotator):
    name = "sam3"
    needs_prompts = True

    def __init__(self, model_ref: str | None = None) -> None:
        self.model_ref = model_ref or settings.SAM3_MODEL
        self._predictor = None
        self._error = ""
        self._lock = threading.Lock()

    # ── loading ───────────────────────────────────────────────────────────────

    def _weights_present(self) -> bool:
        # Unlike other Ultralytics checkpoints these are never fetched for us,
        # so a missing file is the common case and deserves a clear message.
        try:
            return Path(modelcache.resolve(self.model_ref)).expanduser().exists()
        except Exception:
            return False

    def _ensure_predictor(self):
        if self._predictor is not None or self._error:
            return self._predictor
        with self._lock:
            if self._predictor is not None or self._error:
                return self._predictor
            if not self._weights_present():
                self._error = _WEIGHTS_HELP
                return None
            try:
                from ultralytics.models.sam import SAM3SemanticPredictor  # noqa: PLC0415

                self._predictor = SAM3SemanticPredictor(
                    overrides={
                        "conf": settings.SAM3_CONF,
                        "task": "segment",
                        "mode": "predict",
                        "model": str(Path(modelcache.resolve(self.model_ref)).expanduser()),
                        "save": False,
                        "verbose": False,
                    }
                )
            except ImportError:
                self._error = (
                    "SAM 3 needs ultralytics >= 8.3.237. Upgrade with: pip install -U ultralytics"
                )
            except Exception as exc:
                self._error = f"Could not initialise SAM 3: {exc}"
        return self._predictor

    def loaded(self) -> bool:
        return self._predictor is not None or bool(self._error)

    def status(self) -> AnnotatorStatus:
        predictor = self._ensure_predictor()
        if predictor is None:
            return AnnotatorStatus(self.name, False, self._error)
        return AnnotatorStatus(
            self.name,
            True,
            f"{self.model_ref} · open vocabulary, prompt with noun phrases",
            list(settings.SAM3_PROMPTS),
        )

    # ── inference ─────────────────────────────────────────────────────────────

    def annotate(self, images: list[Image.Image], prompts: list[str] | None = None) -> list[list[Detection]]:
        phrases = [p.strip() for p in (prompts or settings.SAM3_PROMPTS) if p.strip()]
        predictor = self._ensure_predictor()
        if predictor is None or not images or not phrases:
            return [[] for _ in images]

        out: list[list[Detection]] = []
        with self._lock:
            for img in images:
                rgb = img.convert("RGB")
                try:
                    # set_image once, then query every concept against the cached
                    # image embedding — the whole reason SAM 3 exposes this API.
                    predictor.set_image(rgb)
                    results = predictor(text=phrases)
                    out.append(self._to_detections(results, phrases, *rgb.size))
                except Exception as exc:
                    # Recorded, not raised: one unreadable frame should not kill
                    # a long run.  annotate_assets turns a run that produced
                    # nothing *but* errors into a failed job.
                    self._error = f"SAM 3 inference failed: {exc}"
                    self.runtime_error = self._error
                    out.append([])
        return out

    def _to_detections(self, results, phrases: list[str], width: int, height: int) -> list[Detection]:
        detections: list[Detection] = []
        # The predictor returns one Results per phrase (or a single Results when
        # given one phrase); normalise both shapes.
        items = results if isinstance(results, (list, tuple)) else [results]
        for phrase, result in zip(phrases, items):
            if result is None:
                continue
            boxes = getattr(result, "boxes", None)
            polygons = self._polygons(result)
            if boxes is None:
                continue
            for i in range(len(boxes)):
                x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i].tolist())
                cx, cy, bw, bh = xyxy_to_norm(x1, y1, x2, y2, width, height)
                conf = float(boxes.conf[i].item()) if getattr(boxes, "conf", None) is not None else 1.0
                detections.append(
                    Detection(
                        label=phrase,
                        conf=conf,
                        x=cx, y=cy, w=bw, h=bh,
                        model=f"sam3:{self.model_ref}",
                        mask=polygons[i] if i < len(polygons) else "",
                    )
                )
        return detections

    @staticmethod
    def _polygons(result) -> list[str]:
        masks = getattr(result, "masks", None)
        if masks is None or getattr(masks, "xyn", None) is None:
            return []
        try:
            return [json.dumps([[round(float(v), 5) for v in poly.flatten()]]) for poly in masks.xyn]
        except Exception:
            return []
