"""Users and login sessions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Identity, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from appdb.base import Base, Str, utcnow

# Ordered least to most privileged; a role passes any check for a role at or
# below it.
ROLES = ("viewer", "curator", "lead", "admin")
ROLE_RANK = {role: rank for rank, role in enumerate(ROLES)}

# The built-in account every request runs as when login is switched off.
LOCAL_USERNAME = "local"


class User(Base):
    __tablename__ = "of_users"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(Str(120), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(Str(255), unique=True)
    # Null for placeholder accounts (e.g. people named in imported history)
    # who have never been given a password and so cannot log in.
    password_hash: Mapped[str | None] = mapped_column(Str(255))
    display_name: Mapped[str | None] = mapped_column(Str(120))
    initials: Mapped[str | None] = mapped_column(Str(10))
    role: Mapped[str] = mapped_column(Str(20), nullable=False, default="curator")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)

    sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True,
    )

    @property
    def label(self) -> str:
        return self.display_name or self.username


class AuthSession(Base):
    __tablename__ = "of_auth_sessions"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("of_users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # SHA-256 of the cookie token. The token itself is never stored, so a
    # leaked database row cannot be replayed as a login.
    token_hash: Mapped[str] = mapped_column(Str(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")
