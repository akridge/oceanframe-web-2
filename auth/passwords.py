"""
Password hashing with bcrypt, called directly.

Not through passlib: passlib's bcrypt handler breaks against bcrypt 4.1+, which
is how CAT ended up calling bcrypt itself. bcrypt only reads the first 72 bytes,
and newer releases raise rather than truncate, so truncate explicitly.
"""
from __future__ import annotations

import bcrypt

MIN_LENGTH = 8


def _prepare(password: str) -> bytes:
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except ValueError:  # a malformed stored hash
        return False


def check_strength(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"Password must be at least {MIN_LENGTH} characters")
