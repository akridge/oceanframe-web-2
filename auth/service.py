"""
Users and sessions: everything the routes and the middleware need.

Functions take an explicit SQLAlchemy session so a route can do several steps in
one transaction; :func:`resolve_token` opens its own because the middleware
runs outside any route.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from appdb.base import blank_to_none, utcnow
from appdb.engine import session_scope
from auth import config
from auth.models import LOCAL_USERNAME, ROLE_RANK, ROLES, AuthSession, User
from auth.passwords import check_strength, hash_password, verify_password

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,119}$")
# How stale last_seen_at may get before a request bothers to update it. Writing
# it on every request would turn each page load into a database write.
TOUCH_INTERVAL = timedelta(minutes=5)


class AuthError(ValueError):
    """A rule was broken; the message is safe to show the user."""


@dataclass(frozen=True)
class CurrentUser:
    """A detached snapshot of the logged-in user, safe to hand across threads."""

    id: int
    username: str
    display_name: str | None
    initials: str | None
    email: str | None
    role: str

    @property
    def label(self) -> str:
        return self.display_name or self.username

    def has_role(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[role]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "username": self.username, "display_name": self.display_name,
            "initials": self.initials, "email": self.email, "role": self.role, "label": self.label,
        }


def snapshot(user: User) -> CurrentUser:
    return CurrentUser(
        id=user.id, username=user.username, display_name=user.display_name,
        initials=user.initials, email=user.email, role=user.role,
    )


def public_user(user: User) -> dict:
    return {
        **snapshot(user).to_dict(),
        "is_active": bool(user.is_active),
        "has_password": bool(user.password_hash),
        "created_at": user.created_at.isoformat() + "Z" if user.created_at else None,
        "last_login": user.last_login.isoformat() + "Z" if user.last_login else None,
    }


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── The built-in local user and the bootstrap admin ─────────────────────────────

def ensure_local_user(session: Session) -> User:
    user = session.scalar(select(User).where(User.username == LOCAL_USERNAME))
    if user is None:
        user = User(username=LOCAL_USERNAME, display_name="Local user", role="admin", is_active=True)
        session.add(user)
        session.flush()
    return user


_local_snapshot: CurrentUser | None = None


def local_user() -> CurrentUser:
    """The user every request runs as when login is off. Cached after first use."""
    global _local_snapshot
    if _local_snapshot is None:
        with session_scope() as session:
            _local_snapshot = snapshot(ensure_local_user(session))
    return _local_snapshot


def reset_cache() -> None:
    global _local_snapshot
    _local_snapshot = None


def ensure_bootstrap_admin(session: Session) -> User | None:
    """Create the configured first admin if it does not exist. Never overwrites."""
    cfg = config.load()
    if not (cfg.admin_username and cfg.admin_password):
        return None
    existing = session.scalar(select(User).where(User.username == cfg.admin_username))
    if existing is not None:
        return existing
    user = User(
        username=cfg.admin_username, email=blank_to_none(cfg.admin_email),
        password_hash=hash_password(cfg.admin_password), role="admin", is_active=True,
        display_name=cfg.admin_username,
    )
    session.add(user)
    session.flush()
    logger.info("Created bootstrap admin %r", cfg.admin_username)
    return user


def starter_users() -> list[tuple[str, str, str]]:
    """(username, role, password) for each well-formed entry in OCEANFRAME_STARTER_USERS: username:role:password
    entries separated by commas (e.g. ``test:lead:Pa1b2,user1:curator:Pc3d4``)."""
    users = []
    for entry in filter(None, (part.strip() for part in os.getenv("OCEANFRAME_STARTER_USERS", "").split(","))):
        username, _, rest = entry.partition(":")
        role, _, password = rest.partition(":")
        if username and role and password:
            users.append((username, role, password))
        else:
            logger.warning("Ignoring starter user %r: expected username:role:password", username or entry[:20])
    return users


def ensure_starter_users(session: Session) -> list[User]:
    """Create the extra accounts in OCEANFRAME_STARTER_USERS. Only missing users are created; an existing
    user's role and password are never touched."""
    created = []
    for username, role, password in starter_users():
        if session.scalar(select(User).where(User.username == username)) is not None:
            continue
        try:
            created.append(create_user(session, username=username, password=password, role=role,
                                       display_name=username))
        except ValueError as exc:  # AuthError, or a weak password
            logger.warning("Starter user %r not created: %s", username, exc)
            continue
        logger.info("Created starter user %r (%s)", username, role)
    return created


def on_bootstrap() -> None:
    reset_cache()
    with session_scope() as session:
        ensure_local_user(session)
        ensure_bootstrap_admin(session)
        ensure_starter_users(session)
        session.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))
        no_admin = config.load().enabled and _active_admins(session) == 0
    if no_admin:
        logger.warning(
            "Login is on but there is no active admin with a password, so nobody can manage users. "
            "Set OCEANFRAME_ADMIN_USERNAME and OCEANFRAME_ADMIN_PASSWORD and restart."
        )


# ── Login sessions ──────────────────────────────────────────────────────────────

_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.is_active or user.username == LOCAL_USERNAME:
        # Still spend the bcrypt time so a missing user is not faster to probe.
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def create_session(session: Session, user: User) -> str:
    cfg = config.load()
    now = utcnow()
    session.execute(delete(AuthSession).where(AuthSession.expires_at < now))
    token = secrets.token_urlsafe(32)
    session.add(AuthSession(
        user_id=user.id, token_hash=_hash_token(token), created_at=now,
        expires_at=now + timedelta(hours=cfg.session_hours), last_seen_at=now,
    ))
    user.last_login = now
    return token


def resolve_token(token: str | None) -> CurrentUser | None:
    """The user a session cookie belongs to, or None if it is unknown, expired or disabled."""
    if not token:
        return None
    now = utcnow()
    with session_scope() as session:
        row = session.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == _hash_token(token))
        ).first()
        if row is None:
            return None
        auth_session, user = row
        if auth_session.expires_at <= now or not user.is_active:
            return None
        if now - auth_session.last_seen_at > TOUCH_INTERVAL:
            auth_session.last_seen_at = now
        return snapshot(user)


def revoke_token(token: str | None) -> None:
    if not token:
        return
    with session_scope() as session:
        session.execute(delete(AuthSession).where(AuthSession.token_hash == _hash_token(token)))


def revoke_user_sessions(session: Session, user_id: int) -> None:
    session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


# ── Managing users ──────────────────────────────────────────────────────────────

def _active_admins(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(User).where(
            User.role == "admin", User.is_active == True, User.username != LOCAL_USERNAME,  # noqa: E712
            User.password_hash.is_not(None),
        )
    ) or 0


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise AuthError(f"Role must be one of: {', '.join(ROLES)}")
    return role


def create_user(
    session: Session, *, username: str, password: str | None, role: str = "curator",
    email: str | None = None, display_name: str | None = None, initials: str | None = None,
    is_active: bool = True,
) -> User:
    username = username.strip()
    if not USERNAME_RE.match(username) or username == LOCAL_USERNAME:
        raise AuthError("Username must be 3-120 letters, digits, dots, dashes or underscores")
    if session.scalar(select(User).where(User.username == username)):
        raise AuthError(f"Username {username!r} is taken")
    email = blank_to_none(email)
    if email and session.scalar(select(User).where(User.email == email)):
        raise AuthError(f"Email {email!r} is already in use")
    if password is not None:
        check_strength(password)
    user = User(
        username=username, email=email, role=_validate_role(role),
        password_hash=hash_password(password) if password else None,
        display_name=blank_to_none(display_name), initials=blank_to_none(initials),
        is_active=is_active,
    )
    session.add(user)
    session.flush()
    return user


def update_user(session: Session, user: User, **fields) -> User:
    """Apply admin edits. Refuses any change that would leave no usable admin."""
    if user.username == LOCAL_USERNAME:
        raise AuthError("The built-in local user cannot be edited")

    losing_admin = user.role == "admin" and user.is_active and (
        ("role" in fields and fields["role"] != "admin")
        or ("is_active" in fields and not fields["is_active"])
    )
    if losing_admin and _active_admins(session) <= 1:
        raise AuthError("That would leave no active admin")

    if "role" in fields and fields["role"] is not None:
        user.role = _validate_role(fields["role"])
    if "email" in fields:
        email = blank_to_none(fields["email"])
        if email and session.scalar(select(User).where(User.email == email, User.id != user.id)):
            raise AuthError(f"Email {email!r} is already in use")
        user.email = email
    for name in ("display_name", "initials"):
        if name in fields:
            setattr(user, name, blank_to_none(fields[name]))
    if "is_active" in fields and fields["is_active"] is not None:
        user.is_active = bool(fields["is_active"])
        if not user.is_active:
            revoke_user_sessions(session, user.id)
    return user


def set_password(session: Session, user: User, new_password: str, *, revoke: bool = True) -> None:
    check_strength(new_password)
    user.password_hash = hash_password(new_password)
    if revoke:
        revoke_user_sessions(session, user.id)
