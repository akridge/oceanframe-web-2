"""
Ultralytics YOLO annotator.

Works with any detect / segment / classify checkpoint — set ``LIB_YOLO_MODEL``
to ``yolo11n.pt`` (default, COCO), ``yolo11n-seg.pt``, or the path to a model
you fine-tuned on your own survey classes.  Detections become both
``detections`` rows (box + confidence, filterable by class and count) and
``class:<name>`` auto-tags.
"""
from __future__ import annotations

import json
import threading

from PIL import Image

from library import modelcache, settings
from library.annotate.base import Annotator, xyxy_to_norm
from library.models import AnnotatorStatus, Detection


class YoloAnnotator(Annotator):
    name = "yolo"

    def __init__(self, model_ref: str | None = None) -> None:
        self.model_ref = model_ref or settings.YOLO_MODEL
        self._model = None
        self._error = ""
        self._lock = threading.Lock()

    # ── loading ───────────────────────────────────────────────────────────────

    def _ensure_model(self):
        if self._model is not None or self._error:
            return self._model
        with self._lock:
            if self._model is None and not self._error:
                try:
                    from ultralytics import YOLO  # noqa: PLC0415

                    self._model = YOLO(modelcache.resolve(self.model_ref))
                except ImportError:
                    self._error = (
                        "ultralytics is not installed. Install it with: pip install ultralytics"
                    )
                except Exception as exc:
                    self._error = f"Could not load '{self.model_ref}': {exc}"
        return self._model

    def loaded(self) -> bool:
        return self._model is not None or bool(self._error)

    def status(self) -> AnnotatorStatus:
        model = self._ensure_model()
        if model is None:
            return AnnotatorStatus(self.name, False, self._error)
        names = getattr(model, "names", {}) or {}
        labels = [str(v) for _, v in sorted(names.items())] if isinstance(names, dict) else list(names)
        return AnnotatorStatus(
            self.name, True, f"{self.model_ref} · {len(labels)} classes", labels
        )

    # ── inference ─────────────────────────────────────────────────────────────

    def annotate(self, images: list[Image.Image], prompts: list[str] | None = None) -> list[list[Detection]]:
        model = self._ensure_model()
        if model is None or not images:
            return [[] for _ in images]

        wanted = {p.strip().lower() for p in (prompts or []) if p.strip()}
        rgb = [im.convert("RGB") for im in images]
        with self._lock:
            results = model.predict(
                rgb, conf=settings.YOLO_CONF, imgsz=settings.YOLO_IMGSZ, verbose=False
            )

        out: list[list[Detection]] = []
        for img, result in zip(rgb, results):
            width, height = img.size
            out.append(self._to_detections(result, width, height, wanted))
        return out

    def _to_detections(self, result, width: int, height: int, wanted: set[str]) -> list[Detection]:
        names = getattr(result, "names", {}) or {}
        detections: list[Detection] = []

        # Classification heads have `probs` and no boxes; record the top-1 as a
        # whole-image detection so classifiers and detectors share one table.
        probs = getattr(result, "probs", None)
        if probs is not None and getattr(result, "boxes", None) is None:
            top = int(probs.top1)
            label = str(names.get(top, top))
            if not wanted or label.lower() in wanted:
                detections.append(
                    Detection(label=label, conf=float(probs.top1conf), x=0.5, y=0.5, w=1.0, h=1.0,
                              model=self.model_ref)
                )
            return detections

        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return detections

        polygons = self._polygons(result)
        for i in range(len(boxes)):
            cls = int(boxes.cls[i].item())
            label = str(names.get(cls, cls))
            if wanted and label.lower() not in wanted:
                continue
            x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i].tolist())
            cx, cy, bw, bh = xyxy_to_norm(x1, y1, x2, y2, width, height)
            detections.append(
                Detection(
                    label=label,
                    conf=float(boxes.conf[i].item()),
                    x=cx, y=cy, w=bw, h=bh,
                    model=self.model_ref,
                    mask=polygons[i] if i < len(polygons) else "",
                )
            )
        return detections

    @staticmethod
    def _polygons(result) -> list[str]:
        """Normalised segmentation polygons as JSON, empty list for detect models."""
        masks = getattr(result, "masks", None)
        if masks is None or getattr(masks, "xyn", None) is None:
            return []
        try:
            return [json.dumps([[round(float(v), 5) for v in poly.flatten()]]) for poly in masks.xyn]
        except Exception:
            return []
