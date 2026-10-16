"""
Curation operations: queries, edits, and the statistics the page shows.

Every write goes through :func:`_record_change`, which bumps the item's
revision, stamps who did it and appends an audit event. That one path is what
makes concurrent curation safe — OASIS let quality review and bulk edits skip
it, so they could overwrite someone else's change without a trace.
"""
from __future__ import annotations

import os
import uuid
from collections import Counter
from pathlib import PurePosixPath
from typing import Iterable, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from appdb.base import blank_to_none, utcnow
from auth.service import CurrentUser
from curation import settings, taxonomy
from curation.splits import split_for
from curation.models import CurationEvaluationRun, CurationEvent, CurationImportOperation, CurationItem, CurationProject
from curation.schemas import (
    CurationBulkUpdate, CurationItemCreate, CurationItemUpdate, CurationPointCreate,
    CurationProjectCreate, CurationQualityBulkRequest, CurationQualityReviewRequest,
)

# Oracle refuses IN lists longer than 1000 (ORA-01795).
IN_CHUNK = 1000


def in_chunks(column, values: Sequence) -> object:
    values = list(values)
    if len(values) <= IN_CHUNK:
        return column.in_(values)
    return or_(*(column.in_(values[i:i + IN_CHUNK]) for i in range(0, len(values), IN_CHUNK)))


def _http(status: int, detail) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


# ── Projects ──────────────────────────────────────────────────────────────────

def create_project(db: Session, request: CurationProjectCreate, user: CurrentUser,
                   source_kind: str | None = None) -> CurationProject:
    source_config = dict(request.source_config)
    source_config["dataset_mode"] = request.dataset_mode.value
    project = CurationProject(
        name=request.name.strip(), description=blank_to_none(request.description),
        curator_name=user.label, owner_user_id=user.id, source_config=source_config,
        **({"source_kind": source_kind} if source_kind else {}),
    )
    from curation import members  # noqa: PLC0415

    db.add(project)
    try:
        db.flush()
        members.ensure_member(db, project.id, user.id, "lead", user.label)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _http(409, "A project with that name already exists") from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(project)
    return project


def get_project_or_404(db: Session, project_id: str) -> CurationProject:
    project = db.get(CurationProject, project_id)
    if project is None:
        raise _http(404, "Curation project not found")
    return project


def get_item_or_404(db: Session, project_id: str, item_id: int, detail: str = "Curation item not found") -> CurationItem:
    item = db.scalar(select(CurationItem).where(
        CurationItem.project_id == project_id, CurationItem.id == item_id,
    ))
    if item is None:
        raise _http(404, detail)
    return item


def delete_project(db: Session, project: CurationProject, confirm_name: str, user: CurrentUser) -> None:
    if confirm_name != project.name:
        raise _http(409, "Project name confirmation does not match")
    if project.owner_user_id not in (None, user.id) and not user.has_role("admin"):
        raise _http(403, "Only the project's owner or an admin can delete it")
    active = db.scalar(select(CurationImportOperation.id).where(
        CurationImportOperation.project_id == project.id,
        CurationImportOperation.status.in_(["queued", "starting", "running"]),
    ).limit(1))
    if active:
        raise _http(409, "Cancel the active import before deleting this project")
    if db.scalar(select(CurationEvaluationRun.id).where(CurationEvaluationRun.project_id == project.id,
                 CurationEvaluationRun.status.in_(["queued", "staging", "running"])).limit(1)):
        raise _http(409, "Cancel the active evaluation before deleting this project")
    from curation import starter_data  # noqa: PLC0415

    starter_data.record_removed(db, project)  # a starter project stays deleted
    db.delete(project)
    db.commit()


# ── Recording changes ─────────────────────────────────────────────────────────

def _state(item: CurationItem) -> dict:
    return {
        "current_label": item.current_label, "included": bool(item.included),
        "review_state": item.review_state, "revision": item.revision,
        "row_px": item.row_px, "column_px": item.column_px, "crop_size": item.crop_size,
        "removed": bool(item.removed),
    }


def check_point_bounds(uri: str, row_px: int, column_px: int) -> None:
    """A point's centre must lie on its source image."""
    from curation import image_cache  # noqa: PLC0415

    try:
        width, height = image_cache.dimensions(uri)
    except FileNotFoundError as exc:
        raise _http(422, "Source image not in the bucket — its points cannot be moved or added") from exc
    if not (0 <= row_px < height and 0 <= column_px < width):
        raise _http(422, f"The point must be inside the image ({width}×{height})")


def set_removed(item: CurationItem, removed: bool) -> None:
    item.removed = removed
    item.included = not removed
    if removed:
        item.review_state = "flagged"


def _event(project_id: str, item_id: Optional[int], event_type: str, user: CurrentUser, *,
           reason: Optional[str] = None, previous=None, new=None, operation_id: Optional[str] = None) -> CurationEvent:
    return CurationEvent(
        project_id=project_id, item_id=item_id, operation_id=operation_id, event_type=event_type,
        user_id=user.id, curator_name=user.label, reason=blank_to_none(reason),
        previous_value=previous, new_value=new,
    )


def _record_change(db: Session, item: CurationItem, user: CurrentUser, event_type: str, previous: dict, *,
                   reason: Optional[str] = None, operation_id: Optional[str] = None, extra_new: Optional[dict] = None) -> None:
    if item.split == "test" and (previous.get("current_label") != item.current_label or
                                 previous.get("included") != bool(item.included)):
        item.metadata_json = {**(item.metadata_json or {}), "test_informed": True}
    item.revision += 1
    item.updated_by_user_id = user.id
    item.updated_by_name = user.label
    item.updated_at = utcnow()
    new = _state(item)
    if (item.metadata_json or {}).get("test_informed"):
        new["test_informed"] = True
    if extra_new:
        new.update(extra_new)
    db.add(_event(item.project_id, item.id, event_type, user, reason=reason,
                  previous=previous, new=new, operation_id=operation_id))


def _check_revision(item: CurationItem, expected: Optional[int]) -> None:
    if expected is not None and item.revision != expected:
        who = f" by {item.updated_by_name}" if item.updated_by_name else ""
        raise _http(409, {
            "message": f"This item was changed{who} since you opened it. Reload to see the latest.",
            "revision": item.revision, "updated_by_name": item.updated_by_name,
        })


def _clean_label(project: CurationProject, raw: str) -> str:
    try:
        return taxonomy.validate_label(raw, project.dataset_mode)
    except ValueError as exc:
        raise _http(422, str(exc)) from exc


# ── Items ─────────────────────────────────────────────────────────────────────

def create_item(db: Session, project_id: str, request: CurationItemCreate, user: CurrentUser) -> CurationItem:
    project = get_project_or_404(db, project_id)
    metadata = dict(request.metadata or {})
    item = CurationItem(
        project_id=project.id, source_key=request.source_key, source_image_uri=request.source_image_uri,
        crop_uri=request.crop_uri, file_name=request.file_name, source_label=request.source_label,
        current_label=taxonomy.effective_label(request.source_label, project.dataset_mode),
        row_px=request.row_px, column_px=request.column_px, crop_size=request.crop_size,
        region=blank_to_none(request.region), site=blank_to_none(request.site), survey_year=request.survey_year,
        island=blank_to_none(str(metadata.get("island") or "")),
        split=request.split or split_for(request.source_image_uri, settings.split_seed(), settings.split_ratios()),
        annotator=blank_to_none(str(metadata.get("annotator") or "")),
        metadata_json={**metadata, "split_origin": "imported" if request.split else "generated"},
        created_by_user_id=user.id, updated_by_user_id=user.id,
        updated_by_name=user.label,
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise _http(409, "This source item already exists") from exc
    project.item_count += 1
    db.add(_event(project.id, item.id, "item_created", user, new=_state(item)))
    db.commit()
    db.refresh(item)
    return item


def item_query(
    db: Session, project_id: str, *,
    item_ids: Optional[Iterable[int]] = None, label: Optional[str] = None,
    labels: Optional[Iterable[str]] = None, review_state: Optional[str] = None,
    quality_decision: Optional[str] = None, included: Optional[bool] = None,
    region: Optional[str] = None, island: Optional[str] = None, year: Optional[int] = None,
    site: Optional[str] = None, image_search: Optional[str] = None, annotator: Optional[str] = None,
    label_origin: Optional[str] = None, source_labels: Optional[Iterable[str]] = None,
    model_labels: Optional[Iterable[str]] = None,
    removed: Optional[str] = None, source_missing: Optional[str] = None,
) -> Query:
    query = db.query(CurationItem).filter(CurationItem.project_id == project_id)
    ids = list(item_ids or [])
    if ids:
        query = query.filter(in_chunks(CurationItem.id, ids))
    if label:
        query = query.filter(CurationItem.current_label == label)
    for column, values in ((CurationItem.current_label, labels), (CurationItem.source_label, source_labels),
                           (CurationItem.model_label, model_labels)):
        chosen = [value for value in (values or []) if value]
        if chosen:
            query = query.filter(in_chunks(column, chosen))
    if review_state:
        query = query.filter(CurationItem.review_state == review_state)
    if quality_decision == "pending":
        query = query.filter(CurationItem.quality_decision.is_(None))
    elif quality_decision:
        query = query.filter(CurationItem.quality_decision == quality_decision)
    if included is not None:
        query = query.filter(CurationItem.included == included)
    for column, value in ((CurationItem.region, region), (CurationItem.island, island),
                          (CurationItem.site, site), (CurationItem.annotator, annotator)):
        if value:
            query = query.filter(column == value)
    if year is not None:
        query = query.filter(CurationItem.survey_year == year)
    term = (image_search or "").strip()
    if term:
        query = query.filter(or_(
            CurationItem.source_image_uri.ilike(f"%{term}%"), CurationItem.site.ilike(f"%{term}%"),
        ))
    if label_origin == "source":
        query = query.filter(CurationItem.current_label == CurationItem.source_label)
    elif label_origin == "classifier":
        query = query.filter(CurationItem.model_label.is_not(None),
                             CurationItem.current_label == CurationItem.model_label)
    elif label_origin == "curated":
        query = query.filter(
            CurationItem.current_label != CurationItem.source_label,
            or_(CurationItem.model_label.is_(None), CurationItem.current_label != CurationItem.model_label),
        )
    for column, mode in ((CurationItem.removed, removed), (CurationItem.source_missing, source_missing)):
        if mode == "hide":
            query = query.filter(column == False)  # noqa: E712
        elif mode == "only":
            query = query.filter(column == True)  # noqa: E712
    return query


def update_item(db: Session, project_id: str, item_id: int, request: CurationItemUpdate,
                user: CurrentUser, *, evaluation_context: Optional[dict] = None) -> CurationItem:
    project = get_project_or_404(db, project_id)
    item = get_item_or_404(db, project_id, item_id)
    _check_revision(item, request.expected_revision)
    previous = _state(item)
    if request.included and (item.removed if request.removed is None else request.removed):
        raise _http(422, "Restore this point before including it")
    if request.current_label is not None:
        item.current_label = _clean_label(project, request.current_label)
    if request.included is not None:
        item.included = request.included
    if request.review_state is not None:
        item.review_state = request.review_state
    geometry = {k: getattr(request, k) for k in ("row_px", "column_px", "crop_size") if getattr(request, k) is not None}
    if geometry:
        if item.row_px is None or item.column_px is None:
            raise _http(422, "This item is a patch with no source coordinates; its geometry cannot be edited")
        row, column = geometry.get("row_px", item.row_px), geometry.get("column_px", item.column_px)
        if "row_px" in geometry or "column_px" in geometry:
            check_point_bounds(item.source_image_uri, row, column)
        item.row_px, item.column_px = row, column
        item.crop_size = geometry.get("crop_size", item.crop_size)
        item.metadata_json = {**(item.metadata_json or {}), "geometry_edited": True}
    if request.removed is not None:
        set_removed(item, request.removed)
    if evaluation_context and evaluation_context.get("split") == "test":
        item.metadata_json = {**(item.metadata_json or {}), "test_informed": True}
    _record_change(db, item, user, "item_updated", previous, reason=request.reason,
                   extra_new={"evaluation": evaluation_context} if evaluation_context else None)
    db.commit()
    db.refresh(item)
    return item


def bulk_update_items(db: Session, project_id: str, request: CurationBulkUpdate,
                      user: CurrentUser) -> tuple[str, int, list[dict]]:
    project = get_project_or_404(db, project_id)
    if request.included is None and request.review_state is None and request.current_label is None:
        raise _http(422, "Choose an inclusion, review-state, or label change")
    new_label = _clean_label(project, request.current_label) if request.current_label is not None else None

    refs = request.refs()
    items = db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id, in_chunks(CurationItem.id, list(refs)),
    )).all()
    operation_id = str(uuid.uuid4())
    updated, conflicts = 0, []
    for item in items:
        expected = refs.get(item.id)
        if expected is not None and item.revision != expected:
            conflicts.append({"id": item.id, "revision": item.revision, "updated_by_name": item.updated_by_name})
            continue
        if request.included and item.removed:  # restore it first; reported like a stale item
            conflicts.append({"id": item.id, "revision": item.revision, "updated_by_name": item.updated_by_name})
            continue
        previous = _state(item)
        if request.included is not None:
            item.included = request.included
        if request.review_state is not None:
            item.review_state = request.review_state
        if new_label is not None:
            item.current_label = new_label
        _record_change(db, item, user, "bulk_item_updated", previous,
                       reason=request.reason, operation_id=operation_id)
        updated += 1
    db.commit()
    return operation_id, updated, conflicts


def _add_point(db: Session, project: CurationProject, anchor: CurationItem, *, row_px: int, column_px: int,
               crop_size: int, label: str, user: CurrentUser, reason: Optional[str] = None,
               operation_id: Optional[str] = None) -> CurationItem:
    """Add a manual point on the anchor's source image. Does not commit. Callers that know the image check bounds."""
    label = _clean_label(project, label)
    token = uuid.uuid4().hex
    crop_name = f"MANUAL_{label}_{PurePosixPath(anchor.source_image_uri).stem}_{token[:8]}.png"
    source_root = anchor.source_image_uri.rsplit("/images/", 1)[0]
    item = CurationItem(
        project_id=project.id, source_key=f"manual:{token}", source_image_uri=anchor.source_image_uri,
        crop_uri=f"{source_root}/generated_crops/{label}/{crop_name}", file_name=crop_name,
        source_label=label, current_label=label, included=True, review_state="reviewed",
        row_px=row_px, column_px=column_px, crop_size=crop_size,
        region=anchor.region, island=anchor.island, site=anchor.site, survey_year=anchor.survey_year,
        split=anchor.split,  # same photo, same split
        annotator=user.label,
        metadata_json={**(anchor.metadata_json or {}), "annotator": user.label,
                       "manual_point": True, "parent_item_id": anchor.id},
        created_by_user_id=user.id, updated_by_user_id=user.id, updated_by_name=user.label,
    )
    db.add(item)
    db.flush()
    project.item_count += 1
    db.add(_event(project.id, item.id, "manual_point_created", user, reason=reason, operation_id=operation_id, new={
        "label": label, "row_px": row_px, "column_px": column_px, "crop_size": crop_size,
    }))
    return item


def create_manual_point(db: Session, project_id: str, anchor_item_id: int, request: CurationPointCreate,
                        user: CurrentUser) -> CurationItem:
    project = get_project_or_404(db, project_id)
    if project.source_kind == "huggingface":  # its items are patches with no source image to point at
        raise HTTPException(422, "This project holds patches only; add points in a project with source images")
    anchor = get_item_or_404(db, project_id, anchor_item_id, "Source image not found")
    item = _add_point(db, project, anchor, row_px=request.row_px, column_px=request.column_px,
                      crop_size=request.crop_size, label=request.label, user=user, reason=request.reason)
    db.commit()
    db.refresh(item)
    return item


def _apply_quality(db: Session, item: CurationItem, user: CurrentUser, *, decision: str, score: Optional[int],
                   flags: list[str], reviewed_at, whole_image: bool, operation_id: Optional[str]) -> None:
    previous = {**_state(item), "quality_decision": item.quality_decision}
    item.quality_score = score if score is not None else item.quality_score
    item.quality_flags = list(flags)
    item.quality_decision = decision
    item.quality_reviewed_by = user.label
    item.quality_reviewed_at = reviewed_at
    metadata_key = "quality_review" if whole_image else "patch_quality_review"
    item.metadata_json = {**(item.metadata_json or {}), metadata_key: {
        "decision": decision, "score": item.quality_score, "flags": list(flags),
        "reviewed_by": user.label, "reviewed_at": reviewed_at.isoformat() + "Z",
    }}
    # Include and Exclude decide inclusion; Hold parks the item for a second
    # look and leaves inclusion as it was.
    if decision == "accept":
        item.included = True
    elif decision == "reject":
        item.included = False
    item.review_state = "reviewed" if decision == "accept" else "flagged"
    _record_change(db, item, user, "quality_reviewed", previous, operation_id=operation_id, extra_new={
        "quality_decision": decision, "quality_score": item.quality_score,
        "scope": "source_image" if whole_image else "patch",
    })


def _quality_targets(db: Session, project_id: str, anchor: CurationItem, whole_image: bool) -> list[CurationItem]:
    if not whole_image:
        return [anchor]
    return list(db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id, CurationItem.source_image_uri == anchor.source_image_uri,
        CurationItem.removed == False,  # noqa: E712 — removed points are not part of the dataset
    ).order_by(CurationItem.id)).all())


def review_quality(db: Session, project_id: str, anchor_item_id: int, request: CurationQualityReviewRequest,
                   user: CurrentUser, *, whole_image: bool) -> CurationItem:
    """Include, exclude or hold an item — or every point on its source image.

    A quality decision also decides inclusion and review state, so it goes
    through the same revision check and audit trail as any other edit.
    """
    get_project_or_404(db, project_id)
    anchor = get_item_or_404(db, project_id, anchor_item_id,
                             "Source image not found" if whole_image else "Curation item not found")
    _check_revision(anchor, request.expected_revision)
    if not whole_image and anchor.removed and request.decision == "accept":
        raise _http(422, "Restore this point before including it")
    reviewed_at = utcnow()
    operation_id = str(uuid.uuid4()) if whole_image else None
    for item in _quality_targets(db, project_id, anchor, whole_image):
        _apply_quality(db, item, user, decision=request.decision, score=request.score, flags=request.flags,
                       reviewed_at=reviewed_at, whole_image=whole_image, operation_id=operation_id)
    db.commit()
    db.refresh(anchor)
    return anchor


def bulk_review_quality(db: Session, project_id: str, request: CurationQualityBulkRequest,
                        user: CurrentUser) -> tuple[str, int, list[dict]]:
    """Many quality decisions under one operation; stale revisions are skipped and reported."""
    get_project_or_404(db, project_id)
    ids = [decision.item_id for decision in request.decisions]
    if len(set(ids)) != len(ids):
        raise _http(422, "Each item may appear only once")
    anchors = {item.id: item for item in db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id, in_chunks(CurationItem.id, ids))).all()}
    missing = [item_id for item_id in ids if item_id not in anchors]
    if missing:
        raise _http(404, f"Curation item not found: {', '.join(map(str, missing[:10]))}")
    whole_image = request.scope == "source"
    # Check every revision before applying anything: a whole-image decision bumps
    # the revision of sibling anchors, which must not conflict with later decisions.
    conflicts, accepted = [], []
    for decision in request.decisions:
        anchor = anchors[decision.item_id]
        if decision.expected_revision is not None and anchor.revision != decision.expected_revision:
            conflicts.append({"id": anchor.id, "revision": anchor.revision, "updated_by_name": anchor.updated_by_name})
        elif anchor.removed and decision.decision == "accept":  # restore first; reported like a stale item
            conflicts.append({"id": anchor.id, "revision": anchor.revision, "updated_by_name": anchor.updated_by_name})
        else:
            accepted.append(decision)
    operation_id = str(uuid.uuid4())
    reviewed_at = utcnow()
    for decision in accepted:
        anchor = anchors[decision.item_id]
        for item in _quality_targets(db, project_id, anchor, whole_image):
            _apply_quality(db, item, user, decision=decision.decision, score=decision.score, flags=decision.flags,
                           reviewed_at=reviewed_at, whole_image=whole_image, operation_id=operation_id)
    db.commit()
    return operation_id, len(accepted), conflicts


# ── Read models ───────────────────────────────────────────────────────────────

def project_stats(db: Session, project_id: str) -> dict:
    get_project_or_404(db, project_id)
    base = CurationItem.project_id == project_id
    total = db.scalar(select(func.count()).select_from(CurationItem).where(base)) or 0
    states = dict(db.execute(
        select(CurationItem.review_state, func.count()).where(base).group_by(CurationItem.review_state)
    ).all())
    class_rows = db.execute(
        select(CurationItem.current_label, func.count().label("n"))
        .where(base, CurationItem.included == True)  # noqa: E712 — renders portably
        .group_by(CurationItem.current_label).order_by(func.count().desc(), CurationItem.current_label)
    ).all()
    included = sum(count for _, count in class_rows)
    return {
        "total": total, "included": included, "excluded": total - included,
        "reviewed": states.get("reviewed", 0), "unreviewed": states.get("unreviewed", 0),
        "flagged": states.get("flagged", 0), "class_counts": {label: count for label, count in class_rows},
    }


def curator_contributions(db: Session, project_id: str) -> list[dict]:
    """Per person: how many changes, on how many distinct items, and when last."""
    rows = db.execute(
        select(
            CurationEvent.user_id,
            func.max(CurationEvent.curator_name),
            func.count(),
            func.count(func.distinct(CurationEvent.item_id)),
            func.max(CurationEvent.created_at),
        )
        .where(CurationEvent.project_id == project_id, CurationEvent.item_id.is_not(None),
               CurationEvent.event_type != "item_created")
        .group_by(CurationEvent.user_id)
        .order_by(func.count().desc())
    ).all()
    return [
        {"user_id": user_id, "name": name, "changes": changes, "items": items, "last_change_at": last}
        for user_id, name, changes, items, last in rows
    ]


def dataset_status(db: Session, project_id: str) -> dict:
    get_project_or_404(db, project_id)
    base = CurationItem.project_id == project_id
    label_rows = db.execute(
        select(
            CurationItem.current_label, func.count(),
            func.sum(case((CurationItem.included == True, 1), else_=0)),  # noqa: E712
            func.sum(case((CurationItem.review_state == "reviewed", 1), else_=0)),
            func.count(func.distinct(CurationItem.source_image_uri)),
            func.sum(case((CurationItem.model_label.is_not(None), 1), else_=0)),
        ).where(base).group_by(CurationItem.current_label)
    ).all()
    region_rows = db.execute(
        select(CurationItem.region, func.count()).where(base, CurationItem.region.is_not(None))
        .group_by(CurationItem.region).order_by(func.count().desc())
    ).all()
    total = sum(row[1] for row in label_rows)
    included = sum(row[2] or 0 for row in label_rows)
    return {
        "total_items": total, "included_items": included, "excluded_items": total - included,
        "source_images": db.scalar(select(func.count(func.distinct(CurationItem.source_image_uri))).where(base)) or 0,
        "reviewed_items": sum(row[3] or 0 for row in label_rows),
        "classifier_predictions": sum(row[5] or 0 for row in label_rows),
        "labels": [{"label": row[0], "total": row[1], "included": row[2] or 0, "reviewed": row[3] or 0,
                    "source_images": row[4]} for row in label_rows],
        "regions": [{"value": row[0], "count": row[1]} for row in region_rows],
        "curators": curator_contributions(db, project_id),
    }


def activity(db: Session, project_id: str, limit: int = 100) -> dict:
    get_project_or_404(db, project_id)
    events = db.scalars(
        select(CurationEvent).where(CurationEvent.project_id == project_id)
        .order_by(CurationEvent.id.desc()).limit(limit)
    ).all()
    return {"events": events, "curators": curator_contributions(db, project_id)}


def source_image_page(query: Query, after_id: int, limit: int, after_missing: int = 0) -> dict:
    """Group filtered items by source image, missing images last, keyset-paged on (missing, first item id)."""
    first_id = func.min(CurationItem.id)
    missing = func.max(case((CurationItem.source_missing == True, 1), else_=0))  # noqa: E712
    grouped = query.with_entities(
        CurationItem.source_image_uri.label("source_image_uri"),
        first_id.label("first_item_id"),
        missing.label("missing"),
        func.min(CurationItem.region).label("region"),
        func.min(CurationItem.site).label("site"),
        func.min(CurationItem.survey_year).label("survey_year"),
        func.count(CurationItem.id).label("annotation_count"),
        func.sum(case((CurationItem.review_state == "reviewed", 1), else_=0)).label("reviewed_count"),
        func.sum(case((CurationItem.included == True, 1), else_=0)).label("included_count"),  # noqa: E712
    ).group_by(CurationItem.source_image_uri).having(
        or_(missing > after_missing, and_(missing == after_missing, first_id > after_id)))
    rows = grouped.order_by(missing, first_id).limit(limit + 1).all()
    has_more = len(rows) > limit
    page = rows[:limit]

    # Distinct labels per image. OASIS used SQLite's group_concat; collecting
    # them in Python for one page of images works on every backend.
    labels: dict[str, set[str]] = {row.source_image_uri: set() for row in page}
    if labels:
        label_rows = query.with_entities(CurationItem.source_image_uri, CurationItem.current_label).filter(
            in_chunks(CurationItem.source_image_uri, list(labels))
        ).distinct().all()
        for uri, label in label_rows:
            labels[uri].add(label)

    images = [{
        "source_image_uri": row.source_image_uri,
        "image_name": os.path.basename(row.source_image_uri),
        "region": row.region, "site": row.site, "survey_year": row.survey_year,
        "annotation_count": row.annotation_count, "reviewed_count": row.reviewed_count or 0,
        "included_count": row.included_count or 0, "labels": sorted(labels[row.source_image_uri]),
        "first_item_id": row.first_item_id,
    } for row in page]
    return {"images": images, "next_cursor": images[-1]["first_item_id"] if has_more and images else None,
            "next_missing": page[-1].missing if has_more and page else 0, "returned": len(images)}


FACETS = (
    ("regions", CurationItem.region, "region"),
    ("islands", CurationItem.island, "island"),
    ("years", CurationItem.survey_year, "year"),
    ("sites", CurationItem.site, "site"),
    ("labels", CurationItem.current_label, "labels"),
    ("annotators", CurationItem.annotator, "annotator"),
)


# Visibility modes always carry a value; they are not a selection that narrows the facets.
VISIBILITY_FILTERS = ("removed", "source_missing")


def facets(db: Session, project_id: str, filters: dict, limit: int = 5000) -> dict:
    """Counts for each metadata filter.

    With nothing selected, each facet is counted with its *own* filter left out
    so the dropdown keeps offering the alternatives. Once something is
    selected, every facet narrows to the filtered set (a cascade).
    """
    if any(value not in (None, [], "") for key, value in filters.items() if key not in VISIBILITY_FILTERS):
        counters = {name: Counter() for name, _, _ in FACETS}
        rows = item_query(db, project_id, **filters).with_entities(
            *(column for _, column, _ in FACETS)
        ).yield_per(5000)
        for row in rows:
            for (name, _, _), value in zip(FACETS, row):
                if value is not None and value != "":
                    counters[name][str(value)] += 1
        return {
            name: [{"value": value, "count": count}
                   for value, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
            for name, counter in counters.items()
        }

    result = {}
    for name, column, omitted in FACETS:
        rows = (
            item_query(db, project_id, **{**filters, omitted: None})
            # Blank strings are stored as NULL (appdb.blank_to_none), so one
            # IS NOT NULL test covers both — and never compares a number to ''.
            .with_entities(column.label("value"), func.count(CurationItem.id).label("count"))
            .filter(column.is_not(None))
            .group_by(column).order_by(func.count(CurationItem.id).desc(), column)
            .limit(limit).all()
        )
        result[name] = [{"value": str(row.value), "count": row.count} for row in rows]
    return result


def confusion_matrix(db: Session, project: CurationProject, filters: dict, actual_axis: str,
                     normalization: str) -> dict:
    """Current (or original) labels against the imported top-1 classifier suggestion."""
    base = item_query(db, project.id, **filters)
    actual_column = CurationItem.current_label if actual_axis == "current" else CurationItem.source_label
    pair_rows = base.with_entities(actual_column, CurationItem.model_label, func.count(CurationItem.id)) \
        .filter(CurationItem.model_label.is_not(None)) \
        .group_by(actual_column, CurationItem.model_label).all()
    unscored = base.filter(CurationItem.model_label.is_(None)).count()

    pairs: Counter = Counter()
    for actual, predicted, count in pair_rows:
        shown = predicted
        if project.dataset_mode == "T1" and actual_axis == "current":
            shown = taxonomy.project_code(predicted, "T1") or "UC"
        pairs[(str(actual), str(shown))] += int(count)

    row_totals: Counter = Counter()
    column_totals: Counter = Counter()
    for (actual, predicted), count in pairs.items():
        row_totals[actual] += count
        column_totals[predicted] += count
    scored = sum(pairs.values())
    correct = sum(count for (actual, predicted), count in pairs.items() if actual == predicted)

    def value(actual: str, predicted: str, count: int) -> float:
        if normalization == "row":
            return count / row_totals[actual]
        if normalization == "column":
            return count / column_totals[predicted]
        return float(count)

    def axis(totals: Counter) -> list[dict]:
        ordered = sorted(totals, key=lambda label: (-totals[label], label))
        return [{"label": label, "total": totals[label], "correct": pairs.get((label, label), 0),
                 "metric": pairs.get((label, label), 0) / totals[label]} for label in ordered]

    return {
        "matrix_source": "imported_csv", "drilldown_kind": "curation_items",
        "actual_axis": actual_axis, "normalization": normalization,
        "scored_count": scored, "unscored_count": unscored, "correct_count": correct,
        "accuracy": correct / scored if scored else 0.0,
        "model": {"name": "Imported Machine suggestion 1", "source": "regional_csv", "provenance_complete": False},
        "metrics": {},
        "rows": axis(row_totals), "columns": axis(column_totals),
        "cells": [{"actual": a, "predicted": p, "count": c, "value": value(a, p, c)} for (a, p), c in pairs.items()],
    }


def match_items(db: Session, project_id: str, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], list[CurationItem]]:
    """Items keyed by (source label, crop file name): how exported crops find their way home."""
    pairs = list(pairs)
    if not pairs:
        return {}
    file_names = list({file_name for _, file_name in pairs})
    labels = list({label for label, _ in pairs})
    rows = db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id,
        in_chunks(CurationItem.file_name, file_names),
        in_chunks(CurationItem.source_label, labels),
    )).all()
    wanted = set(pairs)
    matches: dict[tuple[str, str], list[CurationItem]] = {}
    for row in rows:
        key = (row.source_label, row.file_name)
        if key in wanted:
            matches.setdefault(key, []).append(row)
    return matches


def attach_curation_links(db: Session, project_id: str, assets: list[dict], mutable: bool) -> list[dict]:
    """Match evaluation/split crops back to curation items by (label, file name)."""
    if not assets:
        return []
    matches = match_items(db, project_id, [(asset["actual_label"], asset["file_name"]) for asset in assets])

    linked = []
    for asset in assets:
        candidates = matches.get((asset["actual_label"], asset["file_name"]), [])
        if len(candidates) == 1:
            item = candidates[0]
            link = {
                "match_status": "unique", "item_id": item.id, "revision": item.revision,
                "current_label": item.current_label, "included": bool(item.included),
                "review_state": item.review_state, "source_image_uri": item.source_image_uri,
                "removed": bool(item.removed),
            }
        else:
            link = {"match_status": "ambiguous" if candidates else "unmatched"}
        linked.append({**asset, "mutable": mutable and link["match_status"] == "unique", "curation_link": link})
    return linked


def mark_interrupted_imports(db: Session) -> int:
    """Imports that were mid-run when the process died will never finish; say so."""
    rows = db.scalars(select(CurationImportOperation).where(
        CurationImportOperation.status.in_(["starting", "running"]),
    )).all()
    for operation in rows:
        operation.status = "interrupted"
        operation.error_message = "The server restarted while this import was running. Start it again; rows already imported are kept."
        operation.completed_at = utcnow()
    return len(rows)

