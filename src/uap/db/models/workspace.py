"""Workspace persistence (Master section 3).

A :class:`~uap.workspace.model.Workspace` is a project/container: it groups the
workflows, runs, artifacts, memory and knowledge belonging to one project. It is
*not* a Domain.

The ORM columns mirror the value object exactly, so
:class:`~uap.workspace.store.WorkspaceStore` can copy fields between the two
without a mapping layer. ``settings`` and ``default_workflow_refs`` are JSONB so
a workspace can pin exact Library refs (``"recon-agent@v3"``) and carry
free-form configuration without a schema change.

The store resolves this model lazily (see ``uap.workspace.store``), so this
module being importable is what makes ``POST /api/workspaces`` work instead of
failing with "no Workspace ORM model".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from uap.db.base import Base

__all__ = ["WorkspaceRow"]

class WorkspaceRow(Base):
    """One workspace row (Master section 3)."""

    __tablename__ = "workspaces"

    #: Stable string id chosen by the creator (the store uses ``str(uuid4())``).
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Absolute path anchoring the workspace's runs/artifacts (optional).
    root_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: active / archived / deleted (soft-delete marker written by the store).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    #: Free-form workspace configuration (Master section 62).
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Exact global-Library refs this workspace pins (Master sections 4, 67).
    default_workflow_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
