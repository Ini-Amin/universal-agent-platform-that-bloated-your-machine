"""Node view persistence (canvas-as-stage).

One row per ``(execution_id, node_id)``: the view a node published as the visible
product of its work — video, markdown, code, image, iframe or whiteboard. The
canvas reads this table so a node's work survives a page reload.

Why a dedicated table instead of reusing the event payloads
----------------------------------------------------------
The event log (``execution_events``) is an append-only lifecycle/trace stream
with a closed canonical vocabulary; the UI replays it to derive node *status*.
Views are a different thing: first-class, mutable canvas state keyed by node,
holding potentially large documents. Storing them as events would (a) bloat the
status replay the UI consumes on every reconnect, (b) require widening the
canonical event vocabulary and its payload schemas, and (c) give no natural
"latest view per node" read — an append-only log would return every historical
view. A dedicated table with ``UNIQUE(execution_id, node_id)`` makes a
republish an idempotent upsert and the read API a single indexed lookup.

``execution_id`` cascades with its execution: a view is meaningless without the
runtime instance it belongs to.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from uap.db.base import Base

__all__ = ["NodeViewRow"]

class NodeViewRow(Base):
    """The view one node published for one execution (canvas-as-stage)."""

    __tablename__ = "node_views"
    __table_args__ = (
        UniqueConstraint("execution_id", "node_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Graph node the view belongs to.
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The view spec (``uap.contracts.NodeView`` projection): kind + payload.
    view: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
