"""Embedder factory with a lazily-built process-wide singleton."""
from __future__ import annotations

import threading

from library import settings
from library.embed.base import Embedder, l2_normalise
from library.embed.hashing import HashingEmbedder

__all__ = ["Embedder", "l2_normalise", "get_embedder", "embedder_status", "warm_embedder", "reset_embedder"]

_lock = threading.Lock()
_instance: Embedder | None = None
_status: dict = {"backend": None, "detail": "not loaded", "supports_text": False}


def _build(backend: str) -> tuple[Embedder, str]:
    """Returns (embedder, detail).  'auto' falls back to hash when CLIP is absent."""
    if backend in {"clip", "auto"}:
        try:
            from library.embed.clip import ClipEmbedder  # noqa: PLC0415

            emb = ClipEmbedder()
            return emb, f"CLIP ready on {emb.device}"
        except Exception as exc:
            if backend == "clip":
                raise
            return HashingEmbedder(), _fallback_detail(exc)
    return HashingEmbedder(), (
        "Hash descriptor selected explicitly (LIB_EMBED_BACKEND=hash). "
        "Image similarity, dedupe and quality all work; text search is off."
    )


def _fallback_detail(exc: Exception) -> str:
    """
    One actionable sentence for the UI banner.

    Two very different failures land here and the message has to tell them
    apart: the packages are missing, or they are installed but the weights
    could not be fetched (an offline host, a proxy, a gated repo).
    """
    from library.embed.clip import ClipUnavailable  # noqa: PLC0415

    common = ("Image similarity, near-duplicate detection and quality scoring all work; "
              "text search does not.")
    if isinstance(exc, ClipUnavailable) and exc.packages_missing:
        return (
            f"CLIP is not installed, so the hash descriptor is in use. {common} "
            "Run `pip install -r requirements-ml.txt`, set LIB_EMBED_BACKEND=clip, and re-embed."
        )
    reason = str(exc).strip().split("\n")[0][:200]
    return (
        "CLIP is installed but could not load its weights, so the hash descriptor is in use. "
        f"{common} Reason: {reason}"
    )


def get_embedder() -> Embedder:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                emb, detail = _build(settings.EMBED_BACKEND.strip().lower())
                _instance = emb
                _status.update(
                    backend=emb.name, dim=emb.dim, detail=detail, supports_text=emb.supports_text
                )
    return _instance


def embedder_status() -> dict:
    """Non-loading status probe used by the UI banner."""
    return dict(_status)


_warming = False


def warm_embedder() -> None:
    """Build the embedder in a background thread, once; returns immediately.

    CLIP may download its weights on first use, which must not hold up the
    request that asked for the status banner.
    """
    global _warming
    with _lock:
        if _instance is not None or _warming:
            return
        _warming = True
        _status.update(detail="loading in the background…")

    def run() -> None:
        global _warming
        try:
            get_embedder()
        except Exception as exc:
            reason = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]
            _status.update(detail=f"could not load: {reason}")
        finally:
            _warming = False

    threading.Thread(target=run, name="warm-embedder", daemon=True).start()


def reset_embedder() -> None:
    """Drop the singleton so a settings change takes effect (used by the CLI)."""
    global _instance
    with _lock:
        _instance = None
        _status.update(backend=None, detail="not loaded", supports_text=False)
