"""
Edits to the points on one source image, applied together: move, resize,
remove, restore and new points. One transaction and one operation_id. An item
someone else changed since the editor loaded it is skipped and reported, not
overwritten. Suggestions lay out candidate patches around a patch, with model
hints when a classifier is available.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.service import CurrentUser
from curation import image_cache, images, scoring, service
from curation.models import CurationItem
from curation.schemas import SourceEditBatch, SuggestRequest


def _image_items(db: Session, project_id: str, anchor: CurationItem) -> dict[int, CurationItem]:
    rows = db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id, CurationItem.source_image_uri == anchor.source_image_uri)).all()
    return {item.id: item for item in rows}


def _dimensions(db: Session, uri: str) -> tuple[int, int]:
    images.require_allowed(db, uri)  # never read outside the curated buckets
    try:
        return image_cache.dimensions(uri)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail="Source image not in the bucket — its points cannot be edited") from exc
    except Exception as exc:  # noqa: BLE001 — corrupt object, storage refusal: a clear 422, not a 500
        raise HTTPException(status_code=422, detail="Source image could not be read — its points cannot be edited") from exc


def apply_batch(db: Session, project_id: str, anchor_item_id: int, batch: SourceEditBatch, user: CurrentUser) -> dict:
    project = service.get_project_or_404(db, project_id)
    anchor = service.get_item_or_404(db, project_id, anchor_item_id, "Source image not found")
    if anchor.row_px is None or project.source_kind == "huggingface":
        raise HTTPException(status_code=422, detail="This project holds patches only; there is no source image to edit")
    width, height = _dimensions(db, anchor.source_image_uri)
    items = _image_items(db, project_id, anchor)

    # Validate everything before changing anything: a refused batch changes nothing.
    for edit in batch.edits:
        if edit.item_id not in items:
            raise HTTPException(status_code=422, detail=f"Item {edit.item_id} is not on this image")
        if edit.op == "move":
            if edit.row_px is None or edit.column_px is None:
                raise HTTPException(status_code=422, detail="A move needs row_px and column_px")
            if not (0 <= edit.row_px < height and 0 <= edit.column_px < width):
                raise HTTPException(status_code=422, detail=f"The point must be inside the image ({width}×{height})")
        if edit.op == "resize" and edit.crop_size is None:
            raise HTTPException(status_code=422, detail="A resize needs crop_size")
    for point in batch.added:
        if not (0 <= point.row_px < height and 0 <= point.column_px < width):
            raise HTTPException(status_code=422, detail=f"The point must be inside the image ({width}×{height})")
        service._clean_label(project, point.label)  # noqa: SLF001 — raises 422 for an unknown code

    operation_id = str(uuid.uuid4())
    applied = 0
    # One revision check per item, decided before anything changes: an item whose
    # edits do not all carry the revision it had when the batch arrived is
    # reported once and none of its edits apply. So a move and a resize of one
    # point apply together or conflict together.
    conflicted: list[int] = []
    for edit in batch.edits:
        if items[edit.item_id].revision != edit.expected_revision and edit.item_id not in conflicted:
            conflicted.append(edit.item_id)
    conflicts = [{"id": items[i].id, "revision": items[i].revision, "updated_by_name": items[i].updated_by_name}
                 for i in conflicted]
    for edit in batch.edits:
        item = items[edit.item_id]
        if item.id in conflicted:
            continue
        previous = service._state(item)  # noqa: SLF001
        if edit.op == "move":
            item.row_px, item.column_px = edit.row_px, edit.column_px
        elif edit.op == "resize":
            item.crop_size = edit.crop_size
        if edit.op in {"move", "resize"}:
            item.metadata_json = {**(item.metadata_json or {}), "geometry_edited": True}
        else:
            service.set_removed(item, edit.op == "remove")
        service._record_change(db, item, user, "item_updated", previous,  # noqa: SLF001
                               reason=batch.reason, operation_id=operation_id)
        applied += 1
    added = [service._add_point(db, project, anchor, row_px=p.row_px, column_px=p.column_px,  # noqa: SLF001
                                crop_size=p.crop_size, label=p.label, user=user, reason=batch.reason,
                                operation_id=operation_id) for p in batch.added]
    db.commit()
    for item in added:
        db.refresh(item)
    return {"operation_id": operation_id, "applied": applied, "added": added, "conflicts": conflicts}


def suggest(db: Session, project_id: str, anchor_item_id: int, request: SuggestRequest) -> dict:
    anchor = service.get_item_or_404(db, project_id, anchor_item_id, "Source image not found")
    items = _image_items(db, project_id, anchor)
    source = items.get(request.item_id)
    if source is None or source.row_px is None:
        raise HTTPException(status_code=422, detail="Choose a point on this image to suggest around")
    width, height = _dimensions(db, anchor.source_image_uri)
    size = request.crop_size or source.crop_size
    # Centre on the point where the editor shows it: an unsaved move counts.
    centre_row, centre_column = source.row_px, source.column_px
    if request.row_px is not None and request.column_px is not None:
        if not (request.row_px < height and request.column_px < width):
            raise HTTPException(status_code=422, detail=f"The point must be inside the image ({width}×{height})")
        centre_row, centre_column = request.row_px, request.column_px
    reach = request.grid // 2
    centres = []
    for dr in range(-reach, reach + 1):
        for dc in range(-reach, reach + 1):
            if dr == 0 and dc == 0:
                continue
            row, column = centre_row + dr * request.spacing_px, centre_column + dc * request.spacing_px
            if 0 <= row < height and 0 <= column < width:
                centres.append((row, column))
    candidates = [{"row_px": r, "column_px": c, "crop_size": size} for r, c in centres]
    model_available = False
    if request.model_name and centres:
        try:
            hints = scoring.classify_points(anchor.source_image_uri, centres, size, request.model_name)
            model_available = True
            for candidate, top in zip(candidates, hints):
                candidate["hints"] = top[:3]
        except HTTPException:
            model_available = False
    return {"candidates": candidates, "model_available": model_available}
