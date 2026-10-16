"""Annotator registry."""
from __future__ import annotations

import threading

from library.annotate.base import Annotator
from library.annotate.sam3 import Sam3Annotator
from library.annotate.yolo import YoloAnnotator

__all__ = ["Annotator", "YoloAnnotator", "Sam3Annotator", "get_annotator", "annotator_statuses"]

_BUILDERS = {"yolo": YoloAnnotator, "sam3": Sam3Annotator}
_lock = threading.Lock()
_cache: dict[str, Annotator] = {}


def get_annotator(name: str, model_ref: str | None = None) -> Annotator:
    """
    Cached per (name, model).  Model weights are expensive to load, and an
    indexing run annotates thousands of images with the same one.
    """
    key = f"{name}:{model_ref or ''}"
    with _lock:
        if key not in _cache:
            builder = _BUILDERS.get(name)
            if builder is None:
                raise ValueError(f"Unknown annotator '{name}'. Available: {', '.join(_BUILDERS)}")
            _cache[key] = builder(model_ref)
        return _cache[key]


_warming: set[str] = set()


def _warm(name: str, annotator: Annotator) -> None:
    try:
        annotator.status()  # loads; any failure is kept on the annotator as its error
    finally:
        with _lock:
            _warming.discard(name)


def annotator_statuses() -> list[dict]:
    """
    Status of every annotator without loading any weights on the caller's
    thread. Ones not loaded yet start loading in a background thread and
    report ``loading`` until they finish.
    """
    out = []
    for name in _BUILDERS:
        annotator = get_annotator(name)
        state = annotator.peek()
        if state.loading:
            with _lock:
                start = name not in _warming
                _warming.add(name)
            if start:
                threading.Thread(target=_warm, args=(name, annotator), name=f"warm-{name}", daemon=True).start()
        out.append(state.to_dict())
    return out
