"""Versioned definition tables (Master sections 2.3, 2.4, 4, 67).

Every important entity is a *pair* of tables:

``*_definitions``
    The stable identity of a resource (its ``id`` and human ``name``). This row
    is created once and is what other rows (executions, references) point at
    when they want "the thing", independent of version.

``*_versions``
    An **immutable** snapshot: ``version`` (monotonic int), ``spec`` (JSONB),
    ``status`` (draft/active/deprecated), ``created_at`` and ``content_hash``.
    A version row is never mutated in place - publishing v2 inserts a new row
    and leaves v1 byte-identical (Master section 2.4: "Updating v3 to v4 must
    NOT silently change workflows using v3"). The unique ``(definition_id,
    version)`` constraint plus a foreign key to the definition enforces this.

The four versioned resources required by Master section 2.4 are modelled here:
Workflows, Agents, Tools and Skills. Context templates / evaluation definitions
follow the same shape when their waves land.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum as SAEnum
from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from uap.db.base import Base

__all__ = [
    "AgentDefinition",
    "AgentVersion",
    "SkillDefinition",
    "SkillVersion",
    "ToolDefinition",
    "ToolVersion",
    "VersionStatus",
    "WorkflowDefinition",
    "WorkflowVersion",
]


class VersionStatus(StrEnum):
    """Lifecycle state of one immutable version row.

    ``DRAFT`` -> ``ACTIVE`` -> ``DEPRECATED``. Only ``ACTIVE`` versions should
    be picked up by new executions; deprecated versions stay readable so old
    executions can still be replayed (Master sections 2.3, 45, 61).
    """

    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


#: Shared native-free enum type. Rendered as ``VARCHAR(16)`` + a CHECK
#: constraint so it behaves identically under PostgreSQL and any test backend.
VERSION_STATUS_TYPE = SAEnum(
    VersionStatus,
    name="version_status_enum",
    native_enum=False,
    create_constraint=True,
    length=16,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    validate_strings=True,
)


class _DefinitionMixin:
    """Stable identity columns shared by every ``*_definitions`` table."""

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class _VersionMixin:
    """Immutable snapshot columns shared by every ``*_versions`` table."""

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    version: Mapped[int] = mapped_column(nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        VERSION_STATUS_TYPE, nullable=False, default=VersionStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: sha256 hex digest of the canonical (sorted-key) JSON spec. Computed in
    #: the repository on insert; stable for identical specs, distinct for
    #: different specs (Master sections 2.4, 50).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


# --------------------------------------------------------------------------- #
# Workflows
# --------------------------------------------------------------------------- #

class WorkflowDefinition(_DefinitionMixin, Base):
    """Stable identity of a workflow (Master section 67)."""

    __tablename__ = "workflow_definitions"

    versions: Mapped[list["WorkflowVersion"]] = relationship(
        back_populates="definition",
        cascade="all, delete-orphan",
        order_by="WorkflowVersion.version",
    )


class WorkflowVersion(_VersionMixin, Base):
    """One immutable workflow version (the execution graph snapshot)."""

    __tablename__ = "workflow_versions"
    __table_args__ = (UniqueConstraint("definition_id", "version"),)

    definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    definition: Mapped[WorkflowDefinition] = relationship(back_populates="versions")


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #

class AgentDefinition(_DefinitionMixin, Base):
    """Stable identity of an agent (Master sections 5, 67)."""

    __tablename__ = "agent_definitions"

    versions: Mapped[list["AgentVersion"]] = relationship(
        back_populates="definition",
        cascade="all, delete-orphan",
        order_by="AgentVersion.version",
    )


class AgentVersion(_VersionMixin, Base):
    """One immutable agent version."""

    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("definition_id", "version"),)

    definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    definition: Mapped[AgentDefinition] = relationship(back_populates="versions")


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

class ToolDefinition(_DefinitionMixin, Base):
    """Stable identity of a tool (Master sections 6, 67)."""

    __tablename__ = "tool_definitions"

    versions: Mapped[list["ToolVersion"]] = relationship(
        back_populates="definition",
        cascade="all, delete-orphan",
        order_by="ToolVersion.version",
    )


class ToolVersion(_VersionMixin, Base):
    """One immutable tool version."""

    __tablename__ = "tool_versions"
    __table_args__ = (UniqueConstraint("definition_id", "version"),)

    definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tool_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    definition: Mapped[ToolDefinition] = relationship(back_populates="versions")


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

class SkillDefinition(_DefinitionMixin, Base):
    """Stable identity of a skill (Master sections 7, 67)."""

    __tablename__ = "skill_definitions"

    versions: Mapped[list["SkillVersion"]] = relationship(
        back_populates="definition",
        cascade="all, delete-orphan",
        order_by="SkillVersion.version",
    )


class SkillVersion(_VersionMixin, Base):
    """One immutable skill version."""

    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("definition_id", "version"),)

    definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skill_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    definition: Mapped[SkillDefinition] = relationship(back_populates="versions")
