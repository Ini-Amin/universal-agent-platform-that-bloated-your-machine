"""Execution checkpoint persistence (Master sections 21, 37, 38, 46).

A durable checkpoint is a serialized runtime snapshot taken after a node
completes, so a disconnected UI or a restarted worker can resume an execution
without re-running committed work (sections 21, 46: "A UI must be able to
reconnect and reconstruct current execution state from durable events/state").

``seq`` is a dense per-execution counter (1, 2, 3, ...) with a
``UNIQUE(execution_id, seq)`` constraint, mirroring the execution event log:
``CHECKPOINT_RESTORED`` restores the highest ``seq`` at or before a resume point.

``state`` holds an ``ExecutionResult``-like snapshot (a JSONB document). It is
opaque to the database - the runtime owns its schema - but it must never contain
unredacted secrets.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from uap.db.base import Base

__all__ = ["ExecutionCheckpoint"]

class ExecutionCheckpoint(Base):
    """One durable, ordered checkpoint of an execution (sections 21, 37, 38)."""

    __tablename__ = "execution_checkpoints"
    __table_args__ = (UniqueConstraint("execution_id", "seq"),)

    #: BIGSERIAL monotonic identity, global insertion order.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Dense per-execution checkpoint number (1, 2, 3, ...).
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The node the execution had reached when this checkpoint was taken.
    node_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Serialized ``ExecutionResult``-like runtime snapshot.
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
