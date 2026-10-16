"""
Auth settings, read from the environment on each call.

Read lazily rather than at import so a test (or an operator flipping
``OCEANFRAME_AUTH``) never has to reload modules for a change to take.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    session_hours: int
    cookie_name: str
    cookie_secure: bool
    allow_signup: bool
    signup_role: str
    origin_check: bool
    admin_username: str
    admin_email: str
    admin_password: str


def load() -> AuthConfig:
    return AuthConfig(
        enabled=_bool("OCEANFRAME_AUTH", False),
        session_hours=max(1, _int("OCEANFRAME_SESSION_HOURS", 168)),
        cookie_name=os.getenv("OCEANFRAME_SESSION_COOKIE", "oceanframe_session"),
        cookie_secure=_bool("OCEANFRAME_SESSION_COOKIE_SECURE", False),
        allow_signup=_bool("OCEANFRAME_ALLOW_SIGNUP", True),
        # What a self-registered account starts as. Capped at curator: lead and
        # admin are only ever granted by an admin.
        signup_role=(os.getenv("OCEANFRAME_SIGNUP_ROLE", "curator").strip().lower()
                     if os.getenv("OCEANFRAME_SIGNUP_ROLE", "curator").strip().lower() in {"viewer", "curator"}
                     else "curator"),
        origin_check=_bool("OCEANFRAME_ORIGIN_CHECK", True),
        admin_username=os.getenv("OCEANFRAME_ADMIN_USERNAME", "").strip(),
        admin_email=os.getenv("OCEANFRAME_ADMIN_EMAIL", "").strip(),
        admin_password=os.getenv("OCEANFRAME_ADMIN_PASSWORD", ""),
    )
