"""User and workspace membership persistence models.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from uap.db.base import Base

__all__ = ["UserRow", "WorkspaceMemberRow"]


class UserRow(Base):
    """One user row with role and hashed API key."""

    __tablename__ = "users"

    #: Stable string id (e.g. 'u-abc123' or caller specified).
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: admin / member / viewer
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member", index=True)
    #: SHA-256 hex digest of the raw API key.
    api_key_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkspaceMemberRow(Base):
    """Membership of a user in a workspace with an assigned role."""

    __tablename__ = "workspace_members"

    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    #: owner / admin / member / viewer
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
