"""Execution state + the durable event store foundation (Master sections 2.3,
21, 27, 37, 38, 45, 46).

Two tables, deliberately separated:

``executions``
    One *runtime instance* of a workflow version (Master section 2.3: "an
    execution is an independent runtime instance"). It references the exact
    ``workflow_version_id`` it was instantiated from, so a definition change
    can never retroactively alter a running/finished execution. Runtime graph
    mutations belong to the execution row, never to the definition.

``execution_events``
    An append-only, per-execution ordered event log (Master sections 45, 46:
    "Events must be durable ... support persistence, replay, resync, filtering,
    event sequence numbers"). ``seq`` is a dense per-execution counter
    (1, 2, 3, ...) with a ``UNIQUE(execution_id, seq)`` constraint, so a UI can
    reconnect with ``read_since(execution_id, after_seq)`` and reconstruct
    state without gaps or duplicates.

The event ``kind`` strings are intentionally open (validated in the domain
layer, not by a DB enum) so new canonical event types from Master section 45
can be introduced without a migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from uap.db.base import Base

__all__ = ["Execution", "ExecutionEvent", "ExecutionStatus"]


class ExecutionStatus(StrEnum):
    """Lifecycle of one runtime execution (Master sections 21, 45).

    ``RUNNING`` -> ``PAUSED``/``AWAITING_APPROVAL`` -> ``COMPLETED`` /
    ``FAILED`` / ``CANCELLED``. ``resume_count`` records how many times the
    execution was resumed from a checkpoint (Master sections 37, 38).
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


EXECUTION_STATUS_TYPE = SAEnum(
    ExecutionStatus,
    name="execution_status_enum",
    native_enum=False,
    create_constraint=True,
    length=24,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    validate_strings=True,
)


class Execution(Base):
    """A single runtime instance of a workflow version (Master sections 2.3, 67)."""

    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    #: Exact version this execution was instantiated from. ``RESTRICT`` keeps
    #: provenance honest: a version that has been executed cannot be deleted.
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[ExecutionStatus] = mapped_column(
        EXECUTION_STATUS_TYPE, nullable=False, default=ExecutionStatus.PENDING, index=True
    )

    #: Runtime input payload for this instance (never the definition spec).
    input: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Final output, populated on success; ``None`` while running.
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: Human-readable failure reason, populated on failure.
    error: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    #: Correlation id shared across the events/traces of one logical request
    #: (Master section 46: "execution correlation").
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    #: How many times this execution was resumed (Master sections 37, 38).
    resume_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Worker holding the lease on this execution (durable-runtime wave,
    #: section 39). ``None`` when no worker owns it. Additive/nullable.
    locked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Last liveness signal from the owning worker (durable-runtime wave,
    #: section 39); a stale heartbeat releases the lease. Additive/nullable.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Requester attribution (audit trail of who asked for this run; NOT an
    #: authentication or security boundary — callers can claim any identity;
    #: nullable so existing rows stay valid, and omitted user_id stays NULL).
    requested_by: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    events: Mapped[list["ExecutionEvent"]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="ExecutionEvent.seq",
    )


class ExecutionEvent(Base):
    """One durable, ordered event in an execution's append-only log."""

    __tablename__ = "execution_events"
    __table_args__ = (UniqueConstraint("execution_id", "seq"),)

    #: BIGSERIAL monotonic identity, global insertion order.
    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Dense per-execution sequence number, allocated inside the writing
    #: transaction (see :meth:`uap.db.repositories.EventRepository.append`).
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Canonical event kind, e.g. ``NodeStarted`` / ``ToolCalled`` (section 45).
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Graph node / agent / tool the event is attached to, when applicable.
    node: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    execution: Mapped[Execution] = relationship(back_populates="events")
