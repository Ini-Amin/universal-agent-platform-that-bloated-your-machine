"""Node views: the work a node published, rendered on the canvas.

A view is first-class canvas state — see :mod:`uap.db.models.view` for why it
lives in its own table rather than in the event log. :class:`NodeViewStore` is
the only writer/reader; it takes an injected :class:`~sqlalchemy.orm.Session`
and never commits, exactly like the repositories.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from uap.contracts import NodeView
from uap.db.models.view import NodeViewRow
from uap.db.repositories import ExecutionRepository
__all__ = ["NodeViewStore", "NODE_VIEW_EVENT_KIND"]

#: Event kind used to mirror a publish into the append-only event log (the UI's
#: live stream can surface a view the moment it is published, without polling).
NODE_VIEW_EVENT_KIND = "node_view"

def _coerce_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

def _project(view: NodeView | Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a view into a JSONB-safe dict (permissive extras preserved)."""

    if isinstance(view, NodeView):
        return view.model_dump(mode="json")
    # A raw mapping: validate through NodeView so ``kind`` is checked, but keep
    # any extra renderer hints the producer supplied.
    return NodeView.model_validate(dict(view)).model_dump(mode="json")

class NodeViewStore:
    """Durable, per-``(execution, node)`` view persistence (canvas-as-stage)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def publish(
        self,
        execution_id: str | uuid.UUID,
        node_id: str,
        view: NodeView | Mapping[str, Any],
    ) -> None:
        """Upsert the view ``node_id`` published for ``execution_id``.

        Republishing the same ``(execution_id, node_id)`` replaces the stored
        view (idempotent), so a node that emits a richer view on a retry wins.
        """

        eid = _coerce_uuid(execution_id)
        repo = ExecutionRepository(self._session)
        row = repo.get(eid) or repo.get_by_correlation_id(eid)
        target_id = row.id if row is not None else eid
        payload = _project(view)
        stmt = pg_insert(NodeViewRow).values(
            execution_id=target_id,
            node_id=str(node_id),
            view=payload,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["execution_id", "node_id"],
            set_={"view": stmt.excluded.view},
        )
        self._session.execute(stmt)
        self._session.flush()

    def list_for_execution(self, execution_id: str | uuid.UUID) -> list[dict[str, Any]]:
        """Return ``[{node_id, view, created_at}]`` for an execution, newest last."""

        eid = _coerce_uuid(execution_id)
        repo = ExecutionRepository(self._session)
        row = repo.get(eid) or repo.get_by_correlation_id(eid)
        target_id = row.id if row is not None else eid
        stmt = (
            select(NodeViewRow)
            .where(NodeViewRow.execution_id == target_id)
            .order_by(NodeViewRow.created_at.asc(), NodeViewRow.node_id.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return [
            {
                "node_id": row.node_id,
                "view": dict(row.view or {}),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
