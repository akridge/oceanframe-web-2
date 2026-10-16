"""
Declarative base and the column types that behave the same on Oracle and SQLite.

The rules these encode, each learnt from a way Oracle differs:

* Every string has a length. Oracle has no unbounded VARCHAR2, and a bare
  ``String()`` fails to compile there while passing every SQLite test.
* JSON goes in a text column (CLOB on Oracle) through :class:`JSONText`. A CLOB
  cannot be grouped, ordered, compared or made DISTINCT, so anything that is
  filtered or faceted on gets promoted to a real ``Str`` column instead of being
  dug out of JSON in SQL.
* Oracle stores ``''`` as NULL. :func:`blank_to_none` normalises at the edge so
  the two backends agree on what an empty value is.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


def Str(length: int) -> String:  # noqa: N802 — reads like a type in mapped_column()
    return String(length)


class JSONText(TypeDecorator):
    """A JSON value stored as text: CLOB on Oracle, TEXT on SQLite."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(value, separators=(",", ":"), default=str)

    def process_result_value(self, value: Any, dialect) -> Any:
        if value is None or value == "":
            return None
        if hasattr(value, "read"):  # an unread LOB
            value = value.read()
        return json.loads(value)


def utcnow() -> datetime:
    """Naive UTC. Both backends store it as-is; Oracle sessions are pinned to UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
