"""
Who is online in curation, and what they are doing.

Each open curation tab says it is there with every poll (static/curation/live.js): its project,
its view and what it has open (an image name, say). A tab not heard from for ONLINE is gone; one
closing says so at once. ``online`` gathers the tabs still there by person, with what each has
open (curation/claims.py) and how much they changed lately, for the Activity tab.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from appdb.base import utcnow
from auth.service import CurrentUser
from curation.change_sets import COMMITTABLE
from curation.models import CurationClaim, CurationEvent, CurationPresence, CurationProject

ONLINE = timedelta(seconds=45)
RECENT = timedelta(minutes=15)
FORGET = timedelta(days=1)
VIEWS = {"source", "crop", "quality", "status", "matrix", "versions", "activity"}


def heartbeat(db: Session, project_id: str, holder: str, user: CurrentUser, view: str, detail: Optional[str]) -> None:
    now = utcnow()
    row = db.get(CurationPresence, holder)
    if row is None or row.user_id != user.id:  # a token is one tab of one person: never take over another's
        if row is not None:
            return
        row = CurationPresence(holder=holder, user_id=user.id)
        db.add(row)
    row.project_id, row.user_name, row.last_seen = project_id, user.label, now
    row.view = view if view in VIEWS else "source"
    row.detail = (detail or "")[:300] or None
    db.execute(delete(CurationPresence).where(CurationPresence.last_seen < now - FORGET))
    db.commit()


def leave(db: Session, holder: str, user: CurrentUser) -> None:
    db.execute(delete(CurationPresence).where(CurationPresence.holder == holder, CurationPresence.user_id == user.id))
    db.commit()


def online(db: Session) -> list[dict]:
    """Everyone with a curation tab open, in any project, most recently active first."""
    now = utcnow()
    tabs = db.execute(
        select(CurationPresence, CurationProject.name).join(CurationProject, CurationProject.id == CurationPresence.project_id)
        .where(CurationPresence.last_seen > now - ONLINE).order_by(CurationPresence.last_seen.desc())).all()
    if not tabs:
        return []
    users = {tab.user_id for tab, _ in tabs}
    held = dict(db.execute(select(CurationClaim.user_id, func.count()).where(
        CurationClaim.user_id.in_(users), CurationClaim.expires_at > now).group_by(CurationClaim.user_id)).all())
    recent = {row.user_id: row for row in db.execute(
        select(CurationEvent.user_id, func.count().label("changes"), func.max(CurationEvent.created_at).label("last"))
        .where(CurationEvent.user_id.in_(users), CurationEvent.created_at > now - RECENT,
               CurationEvent.event_type.in_(COMMITTABLE)).group_by(CurationEvent.user_id))}
    people: dict[int, dict] = {}
    for tab, project_name in tabs:
        person = people.get(tab.user_id)
        if person is None:
            change = recent.get(tab.user_id)
            person = people[tab.user_id] = {
                "user_id": tab.user_id, "name": tab.user_name, "last_seen": tab.last_seen,
                "open_items": held.get(tab.user_id, 0), "recent_changes": change.changes if change else 0,
                "last_change_at": change.last if change else None, "tabs": []}
        person["tabs"].append({"project_id": tab.project_id, "project": project_name, "view": tab.view,
                               "detail": tab.detail, "last_seen": tab.last_seen})
    return list(people.values())
