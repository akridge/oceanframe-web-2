"""
Collaboration API — /api/curation/*: project members, change sets, comparing people.

Kept apart from routers/curation.py, which is already large. Reading is open
to anyone signed in; changing membership needs the project lead role.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from appdb.engine import get_db
from auth.deps import current_user
from auth.models import User
from auth.service import CurrentUser
from curation import change_sets, members, people, service

router = APIRouter(prefix="/curation", tags=["curation-collab"])


class MemberIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    role: str = Field(default="curator", pattern="^(curator|lead)$")


class RoleIn(BaseModel):
    role: str = Field(pattern="^(curator|lead)$")


@router.get("/projects/{project_id}/members")
def list_members(project_id: str, db: Session = Depends(get_db), _user: CurrentUser = Depends(current_user)):
    service.get_project_or_404(db, project_id)
    return members.list_members(db, project_id)


@router.post("/projects/{project_id}/members", status_code=201)
def add_member(project_id: str, body: MemberIn, user: CurrentUser = Depends(members.require_project_lead),
               db: Session = Depends(get_db)):
    return members.add_member(db, project_id, body.username, body.role, user)


@router.patch("/projects/{project_id}/members/{user_id}")
def set_member_role(project_id: str, user_id: int, body: RoleIn,
                    user: CurrentUser = Depends(members.require_project_lead), db: Session = Depends(get_db)):
    return members.set_role(db, project_id, user_id, body.role, user)


@router.delete("/projects/{project_id}/members/{user_id}")
def remove_member(project_id: str, user_id: int, user: CurrentUser = Depends(members.require_project_lead),
                  db: Session = Depends(get_db)):
    members.remove_member(db, project_id, user_id, user)
    return {"ok": True}


@router.get("/users")
def find_users(query: str = Query("", max_length=120), db: Session = Depends(get_db),
               _user: CurrentUser = Depends(current_user)):
    """Active people, for the add-member form. Names only — nothing private."""
    stmt = select(User.id, User.username, User.display_name).where(User.is_active == True)  # noqa: E712
    text = query.strip().lower()
    if text:
        like = f"%{text}%"
        stmt = stmt.where(or_(User.username.ilike(like), User.display_name.ilike(like)))
    rows = db.execute(stmt.order_by(User.username).limit(50)).all()
    return [{"id": i, "username": u, "display_name": d} for i, u, d in rows]


class CommitIn(BaseModel):
    message: str = Field(max_length=2000)
    user_id: Optional[int] = None


@router.get("/projects/{project_id}/change-sets/pending")
def pending_changes(project_id: str, user: str = Query("me"), me: CurrentUser = Depends(current_user),
                    db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    target = me.id if user == "me" else int(user) if user.isascii() and user.isdigit() else None
    if target is None:
        raise HTTPException(status_code=422, detail="user must be 'me' or a user id")
    if target != me.id and members.project_role(db, project_id, me) != "lead":
        raise HTTPException(status_code=403, detail="Only a project lead can see someone else's uncommitted changes")
    return change_sets.pending(db, project_id, target)


@router.get("/projects/{project_id}/change-sets/uncommitted")
def uncommitted_changes(project_id: str, db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    service.get_project_or_404(db, project_id)
    return change_sets.uncommitted(db, project_id)


@router.post("/projects/{project_id}/change-sets", status_code=201)
def commit_changes(project_id: str, body: CommitIn, me: CurrentUser = Depends(members.require_project_curator),
                   db: Session = Depends(get_db)):
    author = body.user_id if body.user_id is not None else me.id
    if author != me.id and members.project_role(db, project_id, me) != "lead":
        raise HTTPException(status_code=403, detail="Only a project lead can commit someone else's changes")
    return change_sets.as_dict(change_sets.commit(db, project_id, author, body.message, me))


@router.get("/projects/{project_id}/change-sets")
def list_change_sets(project_id: str, user_id: Optional[int] = None, since_version: Optional[int] = None,
                     limit: int = Query(50, ge=1, le=200), before: Optional[int] = None,
                     db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    service.get_project_or_404(db, project_id)
    return change_sets.history(db, project_id, user_id=user_id, since_version=since_version, limit=limit, before=before)


@router.get("/projects/{project_id}/change-sets/{change_set_id}")
def change_set_detail(project_id: str, change_set_id: int, offset: int = Query(0, ge=0),
                      limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db),
                      _me: CurrentUser = Depends(current_user)):
    return change_sets.detail(db, project_id, change_set_id, offset, limit)


@router.get("/projects/{project_id}/people/compare")
def compare_people(project_id: str, a: int, b: int, since: Optional[int] = None,
                   db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    service.get_project_or_404(db, project_id)
    return people.compare(db, project_id, a, b, since)
