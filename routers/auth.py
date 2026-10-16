"""
Login and user management — /api/auth/*

People can create their own account, as in CAT (``OCEANFRAME_ALLOW_SIGNUP``,
on by default). A new account starts as ``OCEANFRAME_SIGNUP_ROLE`` — curator
unless set to viewer — and only an admin can raise it to lead or admin. Set
``OCEANFRAME_ALLOW_SIGNUP=off`` to have admins create every account instead.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from appdb.engine import get_db
from auth import config, service
from auth.deps import current_user, require_admin
from auth.models import LOCAL_USERNAME, ROLES, User
from auth.service import AuthError, CurrentUser

router = APIRouter(prefix="/auth", tags=["auth"])

# ── Brute-force brake ───────────────────────────────────────────────────────────
# In-process and approximate, which is enough to make password guessing slow on
# a single-container deployment. Keyed on client + username.
_FAILURE_WINDOW_S = 15 * 60
_MAX_FAILURES = 10
_failures: dict[str, deque] = defaultdict(deque)
_failures_lock = threading.Lock()


def _limit_key(request: Request, username: str) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    client = forwarded or (request.client.host if request.client else "?")
    return f"{client}|{username.strip().lower()}"


def _too_many(key: str) -> bool:
    now = time.monotonic()
    with _failures_lock:
        attempts = _failures[key]
        while attempts and now - attempts[0] > _FAILURE_WINDOW_S:
            attempts.popleft()
        return len(attempts) >= _MAX_FAILURES


def _record_failure(key: str) -> None:
    with _failures_lock:
        _failures[key].append(time.monotonic())


def _clear_failures(key: str) -> None:
    with _failures_lock:
        _failures.pop(key, None)


def reset_rate_limit() -> None:
    with _failures_lock:
        _failures.clear()


# ── Bodies ──────────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class SignupIn(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=255)
    initials: str | None = Field(default=None, max_length=10)


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ProfileIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    initials: str | None = Field(default=None, max_length=10)
    email: str | None = Field(default=None, max_length=255)


class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=256)
    role: str = "curator"
    email: str | None = Field(default=None, max_length=255)
    display_name: str | None = Field(default=None, max_length=120)
    initials: str | None = Field(default=None, max_length=10)


class UserUpdateIn(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    email: str | None = Field(default=None, max_length=255)
    display_name: str | None = Field(default=None, max_length=120)
    initials: str | None = Field(default=None, max_length=10)


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=1, max_length=256)


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _set_cookie(response: Response, token: str) -> None:
    cfg = config.load()
    response.set_cookie(
        cfg.cookie_name, token, max_age=cfg.session_hours * 3600, path="/",
        httponly=True, samesite="lax", secure=cfg.cookie_secure,
    )


# ── Session endpoints ───────────────────────────────────────────────────────────

@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    if not config.load().enabled:
        raise HTTPException(status_code=400, detail="Login is switched off on this deployment")
    key = _limit_key(request, body.username)
    if _too_many(key):
        raise HTTPException(status_code=429, detail="Too many failed attempts — try again in a few minutes")
    user = service.authenticate(db, body.username, body.password)
    if user is None:
        _record_failure(key)
        raise HTTPException(status_code=401, detail="Wrong username or password")
    token = service.create_session(db, user)
    db.commit()
    _clear_failures(key)
    _set_cookie(response, token)
    return {"user": service.snapshot(user).to_dict()}


@router.post("/signup")
def signup(body: SignupIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    cfg = config.load()
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="Sign-in is switched off on this deployment")
    if not cfg.allow_signup:
        raise HTTPException(status_code=403, detail="Accounts are created by an admin here — ask yours for one")
    # The same brake as login: repeated failures (taken names, bad passwords)
    # from one client slow it down; a team signing up together is not blocked.
    key = _limit_key(request, "signup")
    if _too_many(key):
        raise HTTPException(status_code=429, detail="Too many sign-ups from here — try again in a few minutes")
    try:
        user = service.create_user(
            db, username=body.username, password=body.password, role=cfg.signup_role,
            email=body.email, display_name=body.display_name, initials=body.initials,
        )
    except ValueError as exc:
        _record_failure(key)
        raise _bad_request(exc) from exc
    token = service.create_session(db, user)
    db.commit()
    _set_cookie(response, token)
    return {"user": service.snapshot(user).to_dict()}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    cfg = config.load()
    service.revoke_token(request.cookies.get(cfg.cookie_name))
    response.delete_cookie(cfg.cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)) -> dict:
    cfg = config.load()
    return {"user": user.to_dict(), "auth_enabled": cfg.enabled, "roles": list(ROLES)}


@router.put("/password")
def change_password(
    body: PasswordIn, request: Request, response: Response,
    user: CurrentUser = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    row = db.get(User, user.id)
    if row is None or row.username == LOCAL_USERNAME:
        raise HTTPException(status_code=400, detail="This account has no password to change")
    if not service.authenticate(db, row.username, body.old_password):
        raise HTTPException(status_code=400, detail="Current password is wrong")
    try:
        # Other devices are logged out; this one gets a fresh session.
        service.set_password(db, row, body.new_password, revoke=True)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    token = service.create_session(db, row)
    db.commit()
    _set_cookie(response, token)
    return {"ok": True}


@router.put("/profile")
def update_profile(body: ProfileIn, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.get(User, user.id)
    if row is None or row.username == LOCAL_USERNAME:
        raise HTTPException(status_code=400, detail="The built-in local user has no profile")
    try:
        service.update_user(db, row, **body.model_dump(exclude_unset=True))
    except AuthError as exc:
        raise _bad_request(exc) from exc
    db.commit()
    return {"user": service.public_user(row)}


# ── Admin: user management ──────────────────────────────────────────────────────

@router.get("/admin/users", dependencies=[Depends(require_admin)])
def list_users(db: Session = Depends(get_db)) -> dict:
    users = db.scalars(
        select(User).where(User.username != LOCAL_USERNAME).order_by(User.username)
    ).all()
    return {"users": [service.public_user(u) for u in users], "roles": list(ROLES)}


@router.post("/admin/users", dependencies=[Depends(require_admin)])
def create_user(body: UserCreateIn, db: Session = Depends(get_db)) -> dict:
    try:
        user = service.create_user(db, **body.model_dump())
    except ValueError as exc:
        raise _bad_request(exc) from exc
    db.commit()
    return {"user": service.public_user(user)}


def _user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or user.username == LOCAL_USERNAME:
        raise HTTPException(status_code=404, detail="No such user")
    return user


@router.patch("/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def update_user(user_id: int, body: UserUpdateIn, db: Session = Depends(get_db)) -> dict:
    user = _user_or_404(db, user_id)
    try:
        service.update_user(db, user, **body.model_dump(exclude_unset=True))
    except AuthError as exc:
        raise _bad_request(exc) from exc
    db.commit()
    return {"user": service.public_user(user)}


@router.post("/admin/users/{user_id}/reset-password", dependencies=[Depends(require_admin)])
def reset_password(user_id: int, body: ResetPasswordIn, db: Session = Depends(get_db)) -> dict:
    user = _user_or_404(db, user_id)
    try:
        service.set_password(db, user, body.new_password, revoke=True)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    db.commit()
    return {"user": service.public_user(user)}
