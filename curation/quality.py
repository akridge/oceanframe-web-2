"""
Image-quality scores, prepared once per project on the server.

Quality review used to score each patch in the browser as it loaded, 200 at a time, so it could
sort and filter only what was loaded. A lead (or an admin) now runs "Prepare quality scores" once:
a background job, queued like an availability check, scores every patch and keeps the result in
``curation_quality_metrics``. Quality review then filters every patch by score, flag and suggestion
on the server, and scores in the browser only the patches the job has not reached.

The job is resumable: it walks items in id order, one commit per chunk, and skips a patch already
scored for its current geometry (``signature``). Run it again after a restart, or after points were
moved or added, and it does only what is left. ``score_image`` is the server copy of the browser's
``scoreImageQuality`` (static/curation/curation.js); the thresholds must stay the same in both.
"""
from __future__ import annotations

import io
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Optional

import numpy as np
from PIL import Image
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from appdb.base import utcnow
from curation import images, settings
from curation.models import CurationImportOperation, CurationItem, CurationProject, CurationQualityMetric
from curation.service import in_chunks

SOURCE = {"kind": "quality_scores", "name": "Prepare quality scores"}
CHUNK = 256
FLAGS = ("blurry", "soft", "too_dark", "too_bright", "low_contrast", "blue_cast", "color_cast", "washed_out",
         "analysis_failed", "clean")
SUGGESTIONS = ("accept", "manual_review", "reject")


def _round(value: float) -> int:
    return int(math.floor(value + 0.5))  # Math.round, not banker's rounding


def score_image(image: Optional[Image.Image]) -> dict:
    """The browser's scoreImageQuality: metrics on a copy at most 128 px a side, then fixed thresholds."""
    if image is None or not image.width or not image.height:
        return {"score": 0, "flags": ["analysis_failed"], "suggestion": "manual_review", "metrics": None}
    rgb = image.convert("RGB")
    size = (min(128, rgb.width), min(128, rgb.height))
    if size != rgb.size:
        rgb = rgb.resize(size, Image.BILINEAR)
    pixels = np.asarray(rgb, dtype=np.float64)
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    count = max(1, luminance.size)
    mean_brightness = float(luminance.mean())
    contrast = math.sqrt(float(((luminance - mean_brightness) ** 2).mean()))
    laplacian = (4 * luminance[1:-1, 1:-1] - luminance[1:-1, :-2] - luminance[1:-1, 2:]
                 - luminance[:-2, 1:-1] - luminance[2:, 1:-1])
    sharpness = max(0.0, float((laplacian ** 2).mean() - laplacian.mean() ** 2)) if laplacian.size else 0.0
    channels = [float(red.mean()), float(green.mean()), float(blue.mean())]
    color_cast = max(channels) / max(1.0, min(channels))
    blue_bias = channels[2] / max(1.0, sum(channels))
    dark = int((luminance < 20).sum())
    bright = int((luminance > 245).sum())
    score, flags = 100, []
    if sharpness < 20:
        score -= 32
        flags.append("blurry")
    elif sharpness < 45:
        score -= 14
        flags.append("soft")
    if mean_brightness < 55:
        score -= 28
        flags.append("too_dark")
    elif mean_brightness > 205:
        score -= 30
        flags.append("too_bright")
    if contrast < 28:
        score -= 18
        flags.append("low_contrast")
    if color_cast > 1.65 and blue_bias > 0.38:
        score -= 18
        flags.append("blue_cast")
    elif color_cast > 1.8:
        score -= 12
        flags.append("color_cast")
    if bright / count > 0.15:
        score -= 20
        flags.append("washed_out")
    score = max(0, min(100, score))
    suggestion = "accept"
    if score < 55 or len(flags) >= 2:
        suggestion = "manual_review"
    if score < 35 or ("washed_out" in flags and "too_bright" in flags):
        suggestion = "reject"
    return {"score": score, "flags": flags or ["clean"], "suggestion": suggestion,
            "metrics": {"sharpness": _round(sharpness), "brightness": _round(mean_brightness),
                        "contrast": _round(contrast), "color_cast": round(color_cast, 2),
                        "clipped": _round((dark + bright) / count * 100)}}


def signature(item) -> str:
    """The geometry a score belongs to: the stored crop, or where the point is cut from its photo."""
    fresh = images.needs_fresh_crop(item)
    return f"{'' if fresh else item.crop_uri or ''}|{item.row_px}|{item.column_px}|{item.crop_size}|{int(fresh)}"[:400]


def is_job(operation: CurationImportOperation) -> bool:
    sources = operation.sources or []
    return bool(sources) and all(source.get("kind") == SOURCE["kind"] for source in sources)


def queue(db: Session, project_id: str, user=None) -> CurationImportOperation:
    """Queue the job, the way an availability check is queued; one already queued or running is returned."""
    active = db.scalars(select(CurationImportOperation).where(
        CurationImportOperation.project_id == project_id,
        CurationImportOperation.status.in_(("queued", "starting", "running")),
    ).order_by(CurationImportOperation.id)).all()
    for existing in active:
        if is_job(existing):
            return existing
    operation = CurationImportOperation(
        project_id=project_id, sources=[dict(SOURCE)], batch_size=CHUNK,
        created_by_user_id=getattr(user, "id", None), created_by_name=getattr(user, "label", None) or "quality scores",
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


def _score_patch(item, project, buckets) -> dict:
    from curation.evaluation_runner import load_patch  # noqa: PLC0415 — it imports this package's neighbours

    loaded = load_patch(item, project, buckets)
    if isinstance(loaded, str):
        return score_image(None)
    try:
        with Image.open(io.BytesIO(loaded[0])) as image:
            return score_image(image)
    except OSError:
        return score_image(None)


def run(db: Session, operation: CurationImportOperation, cancel: Optional[threading.Event] = None) -> dict:
    """Score every patch of the project not yet scored for its current geometry. Resumable."""
    project_id = operation.project_id
    db_project = db.get(CurationProject, project_id)
    project = SimpleNamespace(dataset_mode=db_project.dataset_mode if db_project else "T3")
    buckets = images.allowed_buckets(db)
    after, seen, scored, failed = 0, 0, 0, 0
    with ThreadPoolExecutor(max_workers=settings.evaluation_fetch_workers(), thread_name_prefix="quality") as pool:
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Quality scoring stopped")
            chunk = db.scalars(select(CurationItem).where(CurationItem.project_id == project_id, CurationItem.id > after)
                               .order_by(CurationItem.id).limit(CHUNK)).all()
            if not chunk:
                break
            after = chunk[-1].id
            ids = [item.id for item in chunk]
            done = {row.item_id: row.signature for row in db.execute(
                select(CurationQualityMetric.item_id, CurationQualityMetric.signature).where(
                    in_chunks(CurationQualityMetric.item_id, ids),
                    CurationQualityMetric.flags != ",analysis_failed,"))}
            todo = [item for item in chunk if done.get(item.id) != signature(item)]
            results = list(pool.map(lambda item: _score_patch(item, project, buckets), todo))
            if todo:
                db.execute(delete(CurationQualityMetric).where(in_chunks(CurationQualityMetric.item_id, [i.id for i in todo])))
            now = utcnow()
            for item, result in zip(todo, results):
                metrics = result["metrics"] or {}
                db.add(CurationQualityMetric(
                    item_id=item.id, project_id=project_id, score=result["score"], suggestion=result["suggestion"],
                    flags="," + ",".join(result["flags"]) + ",", signature=signature(item), computed_at=now,
                    **{key: metrics.get(key) for key in ("sharpness", "brightness", "contrast", "color_cast", "clipped")}))
                scored += 1
                failed += "analysis_failed" in result["flags"]
            seen += len(chunk)
            for item in chunk:
                db.expunge(item)
            operation.processed_rows = seen
            operation.imported_rows = scored
            operation.rejected_rows = failed
            db.commit()
    return {"checked": seen, "scored": scored, "failed": failed}


def summary(db: Session, project_id: str) -> dict:
    """How far scoring has got, and the counts Quality review shows before anything is loaded."""
    where = (CurationQualityMetric.project_id == project_id,)
    total = db.scalar(select(func.count()).select_from(CurationItem).where(CurationItem.project_id == project_id)) or 0
    scored = db.scalar(select(func.count()).select_from(CurationQualityMetric).where(*where)) or 0
    by_suggestion = dict(db.execute(select(CurationQualityMetric.suggestion, func.count())
                                    .where(*where).group_by(CurationQualityMetric.suggestion)).all())
    flags = {flag: db.scalar(select(func.count()).select_from(CurationQualityMetric).where(
        *where, CurationQualityMetric.flags.like(f"%,{flag},%"))) or 0 for flag in FLAGS} if scored else {}
    average = db.scalar(select(func.avg(CurationQualityMetric.score)).where(*where))
    last = db.scalars(select(CurationImportOperation).where(CurationImportOperation.project_id == project_id)
                      .order_by(CurationImportOperation.created_at.desc()).limit(20)).all()
    job = next((operation for operation in last if is_job(operation)), None)
    return {"items": total, "scored": scored, "by_suggestion": by_suggestion, "flags": flags,
            "average": round(float(average)) if average is not None else None,
            "job": None if job is None else {"id": job.id, "status": job.status, "processed": job.processed_rows,
                                             "total": total, "error": job.error_message}}


def scores_for(db: Session, project_id: str, item_ids: list[int]) -> dict[int, dict]:
    rows = db.scalars(select(CurationQualityMetric).where(
        CurationQualityMetric.project_id == project_id, in_chunks(CurationQualityMetric.item_id, item_ids))).all()
    return {row.item_id: as_dict(row) for row in rows}


def as_dict(row: CurationQualityMetric) -> dict:
    return {"score": row.score, "suggestion": row.suggestion, "flags": [f for f in row.flags.split(",") if f],
            "metrics": {"sharpness": row.sharpness, "brightness": row.brightness, "contrast": row.contrast,
                        "colorCast": row.color_cast, "clipped": row.clipped}}
