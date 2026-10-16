"""
Change sets: a person's edits, committed together under a message.

Edits apply to the shared draft the moment they are made; until their author
commits, they are that author's "uncommitted changes". Committing stamps them
with a change set in one conditional UPDATE, so two tabs committing at once
make exactly one change set, and an edit landing mid-commit waits for the next.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from auth.models import User
from auth.service import CurrentUser
from curation.models import CurationChangeSet, CurationDatasetVersion, CurationEvent, CurationItem
from curation.service import in_chunks

COMMITTABLE = ("item_created", "item_updated", "bulk_item_updated", "manual_point_created", "quality_reviewed")
MESSAGE_MAX = 500


def _pending_where(project_id: str, user_id: int) -> list:
    return [CurationEvent.project_id == project_id, CurationEvent.user_id == user_id,
            CurationEvent.change_set_id.is_(None), CurationEvent.event_type.in_(COMMITTABLE)]


def kinds_of(event: CurationEvent) -> set[str]:
    if event.event_type == "item_created":
        return {"added"}
    if event.event_type == "manual_point_created":
        return {"point"}
    before, after = event.previous_value or {}, event.new_value or {}
    kinds = {"quality"} if event.event_type == "quality_reviewed" else set()
    for field, kind in (("current_label", "relabel"), ("included", "inclusion"), ("review_state", "review")):
        if field in before and before.get(field) != after.get(field):
            kinds.add(kind)
    if any(field in before and before.get(field) != after.get(field) for field in ("row_px", "column_px")):
        kinds.add("move")
    if "crop_size" in before and before.get("crop_size") != after.get("crop_size"):
        kinds.add("resize")
    if "removed" in before and before.get("removed") != after.get("removed"):
        kinds.add("remove" if after.get("removed") else "restore")
    if kinds & {"remove", "restore"}:
        kinds.discard("inclusion")  # a remove also flips included; say it once
    return kinds or {"other"}


def _top_classes(db: Session, item_ids: set[int], limit: int = 10) -> list[dict]:
    counts: Counter = Counter()
    ids = list(item_ids)
    for start in range(0, len(ids), 1000):
        counts.update(dict(db.execute(
            select(CurationItem.current_label, func.count()).where(in_chunks(CurationItem.id, ids[start:start + 1000]))
            .group_by(CurationItem.current_label)).all()))
    return [{"label": label, "items": n} for label, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def summarize(db: Session, where: list) -> tuple[dict, int, int]:
    """(summary, event count, highest event id) for the events matching ``where``."""
    kinds: Counter = Counter()
    items: set[int] = set()
    count = max_id = 0
    for event in db.scalars(select(CurationEvent).where(*where).order_by(CurationEvent.id)
                            .execution_options(yield_per=2000)):
        count += 1
        max_id = event.id
        kinds.update(kinds_of(event))
        if event.item_id is not None:
            items.add(event.item_id)
    return {"kinds": dict(kinds), "classes": _top_classes(db, items), "items": len(items)}, count, max_id


def _event_dict(event: CurationEvent) -> dict:
    return {"id": event.id, "item_id": event.item_id, "event_type": event.event_type,
            "curator_name": event.curator_name, "reason": event.reason,
            "previous_value": event.previous_value, "new_value": event.new_value, "created_at": event.created_at}


def as_dict(cs: CurationChangeSet) -> dict:
    return {"id": cs.id, "project_id": cs.project_id, "author_user_id": cs.author_user_id, "author_name": cs.author_name,
            "committed_by_user_id": cs.committed_by_user_id, "committed_by_name": cs.committed_by_name,
            "message": cs.message, "change_count": cs.change_count, "summary": cs.summary or {},
            "created_at": cs.created_at}


def pending(db: Session, project_id: str, user_id: int) -> dict:
    where = _pending_where(project_id, user_id)
    summary, count, _ = summarize(db, where)
    recent = db.scalars(select(CurationEvent).where(*where).order_by(CurationEvent.id.desc()).limit(20)).all()
    return {"count": count, "summary": summary, "recent": [_event_dict(e) for e in recent]}


def commit(db: Session, project_id: str, author_id: int, message: str, committer: CurrentUser) -> CurationChangeSet:
    text = (message or "").strip()
    if not 1 <= len(text) <= MESSAGE_MAX:
        raise HTTPException(status_code=422, detail=f"Write a message of 1–{MESSAGE_MAX} characters")
    author = db.get(User, author_id)
    if author is None:
        raise HTTPException(status_code=404, detail="No such person")
    where = _pending_where(project_id, author_id)
    summary, count, max_id = summarize(db, where)
    if not count:
        raise HTTPException(status_code=409, detail="Nothing left to commit")
    change_set = CurationChangeSet(
        project_id=project_id, author_user_id=author_id, author_name=author.display_name or author.username,
        committed_by_user_id=committer.id if committer.id != author_id else None,
        committed_by_name=committer.label if committer.id != author_id else None,
        message=text, change_count=0, summary=summary)
    db.add(change_set)
    db.flush()
    stamped = db.execute(update(CurationEvent).where(*where, CurationEvent.id <= max_id)
                         .values(change_set_id=change_set.id)
                         .execution_options(synchronize_session=False)).rowcount
    if not stamped:  # another tab committed them in between
        db.rollback()
        raise HTTPException(status_code=409, detail="Nothing left to commit")
    change_set.change_count = stamped
    change_set.summary = summarize(db, [CurationEvent.project_id == project_id,
                                        CurationEvent.change_set_id == change_set.id])[0]
    db.commit()
    db.refresh(change_set)
    return change_set


def _watermark(db: Session, project_id: str, version: int) -> int:
    row = db.scalar(select(CurationDatasetVersion).where(
        CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.version == version))
    if row is None:
        raise HTTPException(status_code=404, detail=f"No version v{version}")
    return row.event_watermark or 0


def history(db: Session, project_id: str, *, user_id: Optional[int] = None, since_version: Optional[int] = None,
            limit: int = 50, before: Optional[int] = None) -> list[dict]:
    stmt = select(CurationChangeSet).where(CurationChangeSet.project_id == project_id)
    if user_id is not None:
        stmt = stmt.where(CurationChangeSet.author_user_id == user_id)
    if before is not None:
        stmt = stmt.where(CurationChangeSet.id < before)
    if since_version is not None:
        after = _watermark(db, project_id, since_version)
        stmt = stmt.where(CurationChangeSet.id.in_(select(CurationEvent.change_set_id).where(
            CurationEvent.project_id == project_id, CurationEvent.id > after,
            CurationEvent.change_set_id.is_not(None)).distinct()))
    rows = db.scalars(stmt.order_by(CurationChangeSet.id.desc()).limit(min(max(limit, 1), 200))).all()
    return [as_dict(cs) for cs in rows]


def detail(db: Session, project_id: str, change_set_id: int, offset: int = 0, limit: int = 200) -> dict:
    change_set = db.get(CurationChangeSet, change_set_id)
    if change_set is None or change_set.project_id != project_id:
        raise HTTPException(status_code=404, detail="Change set not found")
    where = [CurationEvent.project_id == project_id, CurationEvent.change_set_id == change_set_id]
    events = db.scalars(select(CurationEvent).where(*where).order_by(CurationEvent.id)
                        .offset(max(offset, 0)).limit(min(max(limit, 1), 1000))).all()
    item_ids = sorted(i for i in db.scalars(select(CurationEvent.item_id).where(
        *where, CurationEvent.item_id.is_not(None)).distinct()))
    return {**as_dict(change_set), "events": [_event_dict(e) for e in events], "item_ids": item_ids}


def uncommitted(db: Session, project_id: str) -> list[dict]:
    rows = db.execute(select(CurationEvent.user_id, func.max(CurationEvent.curator_name), func.count()).where(
        CurationEvent.project_id == project_id, CurationEvent.change_set_id.is_(None),
        CurationEvent.event_type.in_(COMMITTABLE), CurationEvent.user_id.is_not(None))
        .group_by(CurationEvent.user_id)).all()
    return sorted(({"user_id": u, "name": n or "", "count": c} for u, n, c in rows), key=lambda r: (-r["count"], r["name"] or ""))


def in_window(db: Session, project_id: str, after_event: int, up_to_event: Optional[int]) -> dict:
    """Change sets with events in (after, up_to], and what was left uncommitted there — for change logs."""
    where = [CurationEvent.project_id == project_id, CurationEvent.id > after_event,
             CurationEvent.event_type.in_(COMMITTABLE)]
    if up_to_event is not None:
        where.append(CurationEvent.id <= up_to_event)
    # A subquery, not a Python list: Oracle caps a literal IN list at 1000 items.
    ids = select(CurationEvent.change_set_id).where(*where, CurationEvent.change_set_id.is_not(None))
    sets = db.scalars(select(CurationChangeSet).where(CurationChangeSet.id.in_(ids))
                      .order_by(CurationChangeSet.id)).all()
    left = db.execute(select(func.max(CurationEvent.curator_name), func.count()).where(
        *where, CurationEvent.change_set_id.is_(None), CurationEvent.user_id.is_not(None)).group_by(CurationEvent.user_id)).all()
    return {"change_sets": [{"id": cs.id, "author": cs.author_name, "message": cs.message,
                             "change_count": cs.change_count} for cs in sets],
            "uncommitted": [{"name": name or "", "count": count} for name, count in left]}
