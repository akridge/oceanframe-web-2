"""
Project membership: who may change a project, and who leads it.

Project roles replace the app-wide curator/lead checks on project work: a
member curates (or leads) that project whatever their app-wide role, and a
non-member only reads it. App-wide admins count as lead everywhere and need
no row.
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from appdb.base import utcnow
from appdb.engine import get_db
from auth.deps import current_user
from auth.models import User
from auth.service import CurrentUser
from curation.models import CurationProject, CurationProjectMember

ROLES = ("curator", "lead")


def project_role(db: Session, project_id: str, user: CurrentUser) -> Optional[str]:
    if user.has_role("admin"):
        return "lead"
    return db.scalar(select(CurationProjectMember.role).where(
        CurationProjectMember.project_id == project_id, CurationProjectMember.user_id == user.id))


def roles_for(db: Session, user: CurrentUser) -> dict[str, str]:
    if user.has_role("admin"):
        return {project_id: "lead" for project_id in db.scalars(select(CurationProject.id))}
    return dict(db.execute(select(CurationProjectMember.project_id, CurationProjectMember.role)
                           .where(CurationProjectMember.user_id == user.id)).all())


def lead_names(db: Session, project_id: str) -> list[str]:
    rows = db.execute(select(User.display_name, User.username).join(
        CurationProjectMember, CurationProjectMember.user_id == User.id).where(
        CurationProjectMember.project_id == project_id, CurationProjectMember.role == "lead")
        .order_by(User.username)).all()
    return [display or username for display, username in rows]


def require_project_role(minimum: str) -> Callable:
    if minimum not in ROLES:
        raise ValueError(f"Unknown project role {minimum!r}")

    def dependency(project_id: str, user: CurrentUser = Depends(current_user),
                   db: Session = Depends(get_db)) -> CurrentUser:
        if db.get(CurationProject, project_id) is None:
            raise HTTPException(status_code=404, detail="Curation project not found")
        role = project_role(db, project_id, user)
        if role is None:
            leads = lead_names(db, project_id)
            ask = f" — ask a project lead ({', '.join(leads)}) to add you" if leads else ""
            raise HTTPException(status_code=403, detail=f"You are not a member of this project{ask}")
        if minimum == "lead" and role != "lead":
            raise HTTPException(status_code=403, detail="This needs the project lead role")
        return user

    return dependency


require_project_curator = require_project_role("curator")
require_project_lead = require_project_role("lead")


def ensure_member(db: Session, project_id: str, user_id: int, role: str, added_by: str) -> None:
    """Add a member if missing; never downgrades an existing one. Caller commits."""
    existing = db.get(CurationProjectMember, (project_id, user_id))
    if existing is None:
        db.add(CurationProjectMember(project_id=project_id, user_id=user_id, role=role,
                                     added_by_name=added_by, added_at=utcnow()))
    elif role == "lead" and existing.role != "lead":
        existing.role = "lead"


def list_members(db: Session, project_id: str) -> list[dict]:
    rows = db.execute(select(CurationProjectMember, User.username, User.display_name).join(
        User, User.id == CurationProjectMember.user_id).where(
        CurationProjectMember.project_id == project_id).order_by(User.username)).all()
    return [{"user_id": member.user_id, "username": username, "display_name": display,
             "role": member.role, "added_by_name": member.added_by_name, "added_at": member.added_at}
            for member, username, display in rows]


def _lead_count(db: Session, project_id: str) -> int:
    return db.scalar(select(func.count()).select_from(CurationProjectMember).where(
        CurationProjectMember.project_id == project_id, CurationProjectMember.role == "lead")) or 0


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise HTTPException(status_code=422, detail="Role must be curator or lead")


def add_member(db: Session, project_id: str, username: str, role: str, actor: CurrentUser) -> dict:
    _check_role(role)
    user = db.scalar(select(User).where(User.username == username.strip(), User.is_active == True))  # noqa: E712
    if user is None:
        raise HTTPException(status_code=404, detail=f"No active user named {username!r}")
    if db.get(CurationProjectMember, (project_id, user.id)) is not None:
        raise HTTPException(status_code=409, detail=f"{user.display_name or user.username} is already a member")
    db.add(CurationProjectMember(project_id=project_id, user_id=user.id, role=role,
                                 added_by_name=actor.label, added_at=utcnow()))
    db.commit()
    return next(m for m in list_members(db, project_id) if m["user_id"] == user.id)


def _member_or_404(db: Session, project_id: str, user_id: int) -> CurationProjectMember:
    member = db.get(CurationProjectMember, (project_id, user_id))
    if member is None:
        raise HTTPException(status_code=404, detail="Not a member of this project")
    return member


def _keep_a_lead(db: Session, project_id: str, member: CurationProjectMember, actor: CurrentUser) -> None:
    if member.role == "lead" and _lead_count(db, project_id) <= 1 and not actor.has_role("admin"):
        raise HTTPException(status_code=409, detail="A project needs at least one lead; make someone else lead first")


def set_role(db: Session, project_id: str, user_id: int, role: str, actor: CurrentUser) -> dict:
    _check_role(role)
    member = _member_or_404(db, project_id, user_id)
    if role != "lead":
        _keep_a_lead(db, project_id, member, actor)
    member.role = role
    db.commit()
    return next(m for m in list_members(db, project_id) if m["user_id"] == user_id)


def remove_member(db: Session, project_id: str, user_id: int, actor: CurrentUser) -> None:
    member = _member_or_404(db, project_id, user_id)
    _keep_a_lead(db, project_id, member, actor)
    db.delete(member)
    db.commit()
