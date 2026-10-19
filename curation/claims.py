"""
Who has which items open, so two curators do not review the same thing at once.

A browser tab names the items it has open (the points of the source image on screen, or the
undecided patches loaded in Quality review) and says so again every poll. Each tab is a
``holder``, a token it makes up, so two tabs of one curator do not take each other's items. A
claim lasts ``TTL`` past its last renewal: a closed tab lets go when that runs out, or at once
when the page says so on its way out. Another user's queue leaves claimed items out; a curator's
own claims, from any tab, never count against them. Claims only steer the queues: a save to a
claimed item is still allowed, guarded by its revision like any other.

The change feed tells the other tabs what changed since they last looked, so a list they loaded
earlier can drop work someone else has done.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable, Optional

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from appdb.base import utcnow
from auth.service import CurrentUser
from curation.change_sets import COMMITTABLE
from curation.models import CurationClaim, CurationEvent, CurationItem
from curation.service import in_chunks

TTL = timedelta(seconds=120)
MAX_CLAIM = 1000
FEED_LIMIT = 2000


def others_hold(project_id: str, user_id: int):
    """The ids of the project's items claimed, right now, by someone other than *user_id*."""
    return select(CurationClaim.item_id).where(
        CurationClaim.project_id == project_id, CurationClaim.user_id != user_id, CurationClaim.expires_at > utcnow())


def set_claims(db: Session, project_id: str, holder: str, item_ids: Iterable[int], user: CurrentUser) -> dict:
    """Make *holder*'s claims exactly *item_ids* (the first MAX_CLAIM): take the free ones, renew its
    own, let go of the rest. Items another user holds are left with them and reported."""
    wanted = list(dict.fromkeys(int(i) for i in item_ids))[:MAX_CLAIM]
    now = utcnow()
    expires = now + TTL
    gone = delete(CurationClaim).where(CurationClaim.project_id == project_id, CurationClaim.holder == holder)
    db.execute(gone.where(CurationClaim.item_id.not_in(wanted)) if wanted else gone)  # MAX_CLAIM fits one IN list
    if not wanted:
        db.commit()
        return {"held": [], "claimed": 0}
    db.execute(delete(CurationClaim).where(CurationClaim.project_id == project_id, CurationClaim.expires_at <= now,
                                           in_chunks(CurationClaim.item_id, wanted)))
    current = {row.item_id: row for row in db.execute(select(
        CurationClaim.item_id, CurationClaim.holder, CurationClaim.user_id, CurationClaim.user_name).where(
        CurationClaim.project_id == project_id, in_chunks(CurationClaim.item_id, wanted)))}
    mine = [i for i in wanted if i in current and current[i].holder == holder]
    if mine:
        db.execute(update(CurationClaim).where(CurationClaim.project_id == project_id, CurationClaim.holder == holder,
                                               in_chunks(CurationClaim.item_id, mine)).values(expires_at=expires))
    free = [i for i in wanted if i not in current]
    row = {"project_id": project_id, "holder": holder, "user_id": user.id, "user_name": user.label,
           "expires_at": expires}
    if free:
        try:
            with db.begin_nested():
                db.execute(insert(CurationClaim), [{**row, "item_id": i} for i in free])
        except IntegrityError:  # another tab took one of them in between: claim one at a time
            for item_id in free:
                try:
                    with db.begin_nested():
                        db.execute(insert(CurationClaim), [{**row, "item_id": item_id}])
                except IntegrityError:
                    pass
    db.commit()
    held = db.execute(select(CurationClaim.item_id, CurationClaim.user_name).where(
        CurationClaim.project_id == project_id, CurationClaim.user_id != user.id, CurationClaim.expires_at > now,
        in_chunks(CurationClaim.item_id, wanted))).all()
    return {"held": [{"item_id": r.item_id, "user_name": r.user_name} for r in held],
            "claimed": len(wanted) - len(held)}


def watermark(db: Session, project_id: str) -> int:
    return db.scalar(select(func.max(CurationEvent.id)).where(CurationEvent.project_id == project_id)) or 0


def changes(db: Session, project_id: str, after_id: Optional[int], user: CurrentUser) -> dict:
    """What other users changed after event *after_id*, and what they hold now.

    Without *after_id* only the watermark comes back: a tab reads it before it loads its lists, so
    a change landing in between is replayed rather than missed. Past FEED_LIMIT events the tab is
    told to reload instead."""
    last = watermark(db, project_id)
    held = db.execute(select(CurationClaim.item_id, CurationClaim.user_name, CurationItem.source_image_uri)
                      .join(CurationItem, CurationItem.id == CurationClaim.item_id)
                      .where(CurationClaim.project_id == project_id, CurationClaim.user_id != user.id,
                             CurationClaim.expires_at > utcnow()).limit(FEED_LIMIT)).all()
    claimed = [{"item_id": r.item_id, "user_name": r.user_name, "source_image_uri": r.source_image_uri} for r in held]
    if after_id is None or after_id >= last:
        return {"last_id": last, "items": [], "images": [], "claimed": claimed, "reload": False}
    events = db.execute(select(CurationEvent.id, CurationEvent.item_id).where(
        CurationEvent.project_id == project_id, CurationEvent.id > after_id, CurationEvent.item_id.is_not(None),
        CurationEvent.event_type.in_(COMMITTABLE),
        (CurationEvent.user_id != user.id) | CurationEvent.user_id.is_(None),
    ).order_by(CurationEvent.id).limit(FEED_LIMIT + 1)).all()
    if len(events) > FEED_LIMIT:
        return {"last_id": last, "items": [], "images": [], "claimed": claimed, "reload": True}
    ids = list(dict.fromkeys(e.item_id for e in events))
    items = db.execute(select(
        CurationItem.id, CurationItem.review_state, CurationItem.quality_decision, CurationItem.included,
        CurationItem.removed, CurationItem.source_image_uri, CurationItem.updated_by_name, CurationItem.revision,
    ).where(CurationItem.project_id == project_id, in_chunks(CurationItem.id, ids))).all() if ids else []
    uris = list({row.source_image_uri for row in items})
    # Per changed image: how many points are still to review, so a queue can drop finished images.
    pending = case(((CurationItem.review_state == "unreviewed") & (CurationItem.removed == False), 1), else_=0)  # noqa: E712
    images = db.execute(select(CurationItem.source_image_uri, func.sum(pending).label("pending"))
                        .where(CurationItem.project_id == project_id, in_chunks(CurationItem.source_image_uri, uris))
                        .group_by(CurationItem.source_image_uri)).all() if uris else []
    return {
        "last_id": last, "reload": False, "claimed": claimed,
        "items": [{"id": r.id, "review_state": r.review_state, "quality_decision": r.quality_decision,
                   "included": bool(r.included), "removed": bool(r.removed), "source_image_uri": r.source_image_uri,
                   "updated_by_name": r.updated_by_name, "revision": r.revision} for r in items],
        "images": [{"source_image_uri": r.source_image_uri, "pending": int(r.pending or 0)} for r in images],
    }
