"""Decision trace persistence (Master section 26).

One row per structured operational decision. This is the durable backing store
for :class:`uap.trace.model.DecisionTrace`; the Pydantic contract stays
framework-free and this module maps it onto PostgreSQL.

Rationale is *structured operational rationale* - the row never holds raw
chain-of-thought (section 26). ``inputs_summary`` is a small, redacted mapping
(see :func:`uap.trace.store.redact_secrets`).

``execution_id`` cascades with its execution: traces are meaningless without the
runtime instance they describe.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from uap.contracts import utc_now
from uap.db.base import Base

__all__ = ["DECISION_TYPES", "DecisionTraceRow"]

#: The allowed ``decision_type`` values, mirroring
#: :class:`uap.trace.model.DecisionType`. Kept as plain strings so the DB check
#: constraint does not depend on the domain enum (the value set is stable).
DECISION_TYPES: tuple[str, ...] = (
    "routing",
    "model_selection",
    "tool_selection",
    "scope_check",
    "policy_check",
    "approval",
    "retry",
    "fallback",
    "replan",
    "condition",
    "synthesis",
)

class DecisionTraceRow(Base):
    """A persisted decision trace (Master section 26).

    Named ``DecisionTraceRow`` to avoid colliding with the Pydantic
    :class:`uap.trace.model.DecisionTrace` contract; the table is
    ``decision_traces``.
    """

    __tablename__ = "decision_traces"
    __table_args__ = (
        CheckConstraint(
            "decision_type IN ("
            + ", ".join(f"'{value}'" for value in DECISION_TYPES)
            + ")",
            name="decision_type",
        ),
        Index(
            "ix_decision_traces_execution_id_created_at",
            "execution_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    #: Graph node / agent the decision belongs to, when applicable.
    node_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: One of :data:`DECISION_TYPES` (DB-checked).
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The chosen option (agent / tool / model / branch / verdict / ...).
    chosen: Mapped[str] = mapped_column(Text, nullable=False)
    #: Rejected alternatives: ``[{"option": ..., "reason_rejected": ...}]``.
    alternatives: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: A SHORT structured reason - never raw chain-of-thought (section 26).
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    #: Evidence references: ``[{"kind": ..., "ref": ...}]``.
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: Confidence in the decision (0..1), when expressed.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Small, redacted input summary - never secrets (sections 26, 73).
    inputs_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
