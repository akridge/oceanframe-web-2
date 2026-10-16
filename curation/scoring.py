"""
Classify a point — or a small grid of patches around it — with a YOLO
classification model, to help a curator place and label a new point.

Needs ultralytics, which is only in the ML images; the slim image answers 501.
"""
from __future__ import annotations

import threading
from io import BytesIO

from fastapi import HTTPException
from PIL import Image

from curation import image_cache
from curation.schemas import CurationClassifyRequest
from library import modelcache, settings as library_settings

MAX_PATCHES = 81
_predict_lock = threading.Lock()
_models: dict[str, object] = {}


def available_models() -> list[dict]:
    directory = library_settings.MODEL_DIR
    if not directory.is_dir():
        return []
    return [{"name": path.name, "size": path.stat().st_size} for path in sorted(directory.glob("*.pt"))]


def load_model(name: str):
    try:
        from ultralytics import YOLO  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="Point classification needs the ML image (ultralytics is not installed)") from exc
    if name not in {model["name"] for model in available_models()}:
        raise FileNotFoundError(f"No model named {name!r} in {library_settings.MODEL_DIR}")
    model = _models.get(name)
    if model is None:
        model = _models[name] = YOLO(modelcache.resolve(name))
    return model


def _positions(center: int, radius: int, step: int, maximum: int) -> list[int]:
    if radius == 0:
        return [max(0, min(maximum - 1, center))]
    return sorted({max(0, min(maximum - 1, value)) for value in range(center - radius, center + radius + 1, step)})


def _open_source(source_image_uri: str) -> Image.Image:
    try:
        payload = image_cache.read_source(source_image_uri)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source image not found") from exc
    try:
        return Image.open(BytesIO(payload)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Source image could not be decoded") from exc


def _predict_points(source: Image.Image, points: list[tuple[int, int]], crop_size: int, model_name: str) -> list[list[dict]]:
    half = crop_size // 2
    crops = []
    for row, column in points:
        left = max(0, min(source.width - crop_size, column - half))
        top = max(0, min(source.height - crop_size, row - half))
        crop = source.crop((left, top, left + crop_size, top + crop_size))
        crops.append(crop.resize((224, 224), Image.Resampling.LANCZOS))

    try:
        model = load_model(model_name)
        with _predict_lock:
            results = model.predict(source=crops, imgsz=224, verbose=False)
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Classification failed: {exc}") from exc

    classified = []
    for result in results:
        probabilities = getattr(result, "probs", None)
        if probabilities is None:
            raise HTTPException(status_code=422, detail="Selected model is not an image-classification model")
        names = getattr(result, "names", {}) or getattr(model, "names", {}) or {}
        predictions = []
        for rank, (index, confidence) in enumerate(zip(probabilities.top5, probabilities.top5conf.tolist()), start=1):
            label = names.get(int(index), str(index)) if isinstance(names, dict) else names[int(index)]
            predictions.append({"label": str(label), "confidence": round(float(confidence), 6), "rank": rank})
        classified.append(predictions)
    return classified


def classify_points(uri: str, points: list[tuple[int, int]], crop_size: int, model_name: str) -> list[list[dict]]:
    """Top predictions for a square patch around each (row, column), cut from the cached source."""
    return _predict_points(_open_source(uri), points, crop_size, model_name)


def classify_neighborhood(source_image_uri: str, request: CurationClassifyRequest) -> list[dict]:
    source = _open_source(source_image_uri)
    rows = _positions(request.row_px, request.radius, request.step, source.height)
    columns = _positions(request.column_px, request.radius, request.step, source.width)
    if len(rows) * len(columns) > MAX_PATCHES:
        raise HTTPException(status_code=422, detail=f"Dense classification is limited to {MAX_PATCHES} patches")
    points = [(row, column) for row in rows for column in columns]
    predictions = _predict_points(source, points, request.crop_size, request.model_name)
    return [{"row_px": row, "column_px": column, "predictions": top}
            for (row, column), top in zip(points, predictions)]
