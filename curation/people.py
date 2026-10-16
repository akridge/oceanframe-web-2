"""Comparing two people's work in one project: what each did, overlap, disagreements."""
from __future__ import annotations

from collections import Counter
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.models import User
from curation.change_sets import COMMITTABLE, _top_classes, _watermark, kinds_of
from curation.models import CurationChangeSet, CurationEvent, CurationItem
from curation.service import in_chunks

FIELDS = ("current_label", "included")
MAX_ROWS = 500


def compare(db: Session, project_id: str, a_id: int, b_id: int, since_version: Optional[int]) -> dict:
    if a_id == b_id:
        raise HTTPException(status_code=422, detail="Choose two different people")
    people = {u.id: u for u in db.scalars(select(User).where(User.id.in_([a_id, b_id])))}
    if len(people) != 2:
        raise HTTPException(status_code=404, detail="No such person")
    after = _watermark(db, project_id, since_version) if since_version is not None else 0

    sides, items, latest = {}, {a_id: set(), b_id: set()}, {}
    for uid in (a_id, b_id):
        sides[uid] = {"kinds": Counter(), "changes": 0, "sets": set()}
    events = db.scalars(select(CurationEvent).where(
        CurationEvent.project_id == project_id, CurationEvent.user_id.in_([a_id, b_id]),
        CurationEvent.id > after, CurationEvent.event_type.in_(COMMITTABLE)).order_by(CurationEvent.id)
        .execution_options(yield_per=2000))
    for event in events:
        side = sides[event.user_id]
        side["changes"] += 1
        side["kinds"].update(kinds_of(event))
        if event.change_set_id is not None:
            side["sets"].add(event.change_set_id)
        if event.item_id is None:
            continue
        items[event.user_id].add(event.item_id)
        before, after_value = event.previous_value or {}, event.new_value or {}
        for field in FIELDS:
            if field in before and before.get(field) != after_value.get(field):
                latest[(event.user_id, event.item_id, field)] = (after_value.get(field), event.created_at)

    overlap = items[a_id] & items[b_id]
    rows = []
    for item_id in sorted(overlap):
        for field in FIELDS:
            a, b = latest.get((a_id, item_id, field)), latest.get((b_id, item_id, field))
            if a and b and a[0] != b[0]:
                rows.append({"item_id": item_id, "field": field, "a_value": a[0], "a_at": a[1],
                             "b_value": b[0], "b_at": b[1]})
    current = {}
    if rows:
        wanted = sorted({r["item_id"] for r in rows})
        for item in db.scalars(select(CurationItem).where(CurationItem.id.in_(wanted[:MAX_ROWS]))):
            current[item.id] = item
    for row in rows:
        item = current.get(row["item_id"])
        row["file_name"] = item.file_name if item else None
        row["current"] = (getattr(item, row["field"]) if item else None)

    def person(uid: int) -> dict:
        user, side = people[uid], sides[uid]
        sets = db.scalars(select(CurationChangeSet).where(in_chunks(CurationChangeSet.id, sorted(side["sets"])))
                          .order_by(CurationChangeSet.id)).all() if side["sets"] else []
        return {"user_id": uid, "name": user.display_name or user.username, "changes": side["changes"],
                "kinds": dict(side["kinds"]), "classes": _top_classes(db, items[uid]),
                "change_sets": [{"id": cs.id, "message": cs.message, "change_count": cs.change_count,
                                 "created_at": cs.created_at} for cs in sets]}

    return {"people": [person(a_id), person(b_id)], "overlap": len(overlap),
            "disagreements": {"count": len(rows), "items": rows[:MAX_ROWS],
                              "item_ids": sorted({r["item_id"] for r in rows})}}
