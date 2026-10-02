"""Durable execution checkpoints (Master sections 21, 37, 38, 46).

A checkpoint is a serialized runtime snapshot taken *after a node completes*, so
a disconnected UI or a restarted worker can resume an execution without
re-running committed work (sections 37, 38).

Two storage shapes are supported, in this order:

* **The dedicated ``execution_checkpoints`` table** (``uap.db.models.checkpoint``)
  when that sibling module is present. ``seq`` is a dense per-execution counter
  with ``UNIQUE(execution_id, seq)``; :meth:`CheckpointStore.save` is an *upsert*
  (``INSERT ... ON CONFLICT (execution_id, seq) DO UPDATE``), so re-taking a
  checkpoint at the same seq replaces its state instead of failing.
* **An EventRepository-only fallback** when the table module is absent: the state
  is stored inside a ``checkpoint_saved`` execution event payload. This keeps the
  runtime usable (and the tests importable) even before the checkpoint migration
  lands, without changing the :class:`CheckpointStore` interface.

The store never commits - it participates in the caller's
:func:`uap.db.engine.session_scope` transaction, exactly like the repositories.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from uap.db.repositories import EventRepository

try:  # pragma: no cover - exercised implicitly by whichever branch is active
    from uap.db.models.checkpoint import ExecutionCheckpoint

    CHECKPOINTS_AVAILABLE = True
except Exception:  # pragma: no cover - fallback path
    ExecutionCheckpoint = None  # type: ignore[assignment]
    CHECKPOINTS_AVAILABLE = False

__all__ = ["CheckpointStore", "CHECKPOINTS_AVAILABLE", "CHECKPOINT_EVENT_KIND"]

#: Event ``kind`` used by the EventRepository-only fallback mode.
CHECKPOINT_EVENT_KIND = "checkpoint_saved"

def _jsonable(value: Any) -> Any:
    """Return a JSONB-safe projection of ``value`` (best effort, never raises).

    Runtime snapshots can contain pydantic models, enums or arbitrary objects.
    ``default=str`` makes the projection lossless-enough for resume and always
    serializable; anything truly unserializable degrades to its ``repr``.
    """

    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {"_repr": str(value)}

def _coerce_uuid(execution_id: str | uuid.UUID) -> uuid.UUID:
    return execution_id if isinstance(execution_id, uuid.UUID) else uuid.UUID(str(execution_id))

class CheckpointStore:
    """Durable checkpoint persistence for one execution (sections 21, 37, 38).

    Constructed with an injected :class:`~sqlalchemy.orm.Session`; it holds no
    other state and never commits (section 73: no hidden singletons).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def save(
        self,
        execution_id: str,
        seq: int,
        state: Mapping[str, Any],
        node_id: str | None = None,
    ) -> None:
        """Upsert the checkpoint ``(execution_id, seq)`` with ``state``.

        Re-saving the same ``seq`` *replaces* the stored state (and node id)
        rather than raising - checkpoints are idempotent recovery points.
        """

        eid = _coerce_uuid(execution_id)
        payload = _jsonable(dict(state))

        if CHECKPOINTS_AVAILABLE:
            stmt = pg_insert(ExecutionCheckpoint).values(
                execution_id=eid,
                seq=int(seq),
                node_id=node_id,
                state=payload,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["execution_id", "seq"],
                set_={"state": stmt.excluded.state, "node_id": stmt.excluded.node_id},
            )
            self._session.execute(stmt)
            self._session.flush()
            return

        # Fallback: durable checkpoint inside the append-only event log.
        EventRepository(self._session).append(
            eid,
            CHECKPOINT_EVENT_KIND,
            node=node_id,
            payload={"seq": int(seq), "node_id": node_id, "state": payload},
        )

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #

    def latest(self, execution_id: str) -> tuple[int, dict] | None:
        """Return ``(seq, state)`` for the highest-seq checkpoint, or ``None``."""

        eid = _coerce_uuid(execution_id)

        if CHECKPOINTS_AVAILABLE:
            stmt = (
                select(ExecutionCheckpoint.seq, ExecutionCheckpoint.state)
                .where(ExecutionCheckpoint.execution_id == eid)
                .order_by(ExecutionCheckpoint.seq.desc())
                .limit(1)
            )
            row = self._session.execute(stmt).first()
            if row is None:
                return None
            return int(row[0]), dict(row[1] or {})

        events = EventRepository(self._session).read_since(eid, 0)
        found = [
            (int(e.payload.get("seq", 0)), dict(e.payload.get("state", {})))
            for e in events
            if e.kind == CHECKPOINT_EVENT_KIND
        ]
        if not found:
            return None
        return max(found, key=lambda item: item[0])

    def list_for_execution(self, execution_id: str) -> list[tuple[int, dict]]:
        """Return every checkpoint as ``(seq, state)``, ascending by ``seq``."""

        eid = _coerce_uuid(execution_id)

        if CHECKPOINTS_AVAILABLE:
            stmt = (
                select(ExecutionCheckpoint.seq, ExecutionCheckpoint.state)
                .where(ExecutionCheckpoint.execution_id == eid)
                .order_by(ExecutionCheckpoint.seq.asc())
            )
            return [
                (int(seq), dict(state or {}))
                for seq, state in self._session.execute(stmt).all()
            ]

        events = EventRepository(self._session).read_since(eid, 0)
        found = [
            (int(e.payload.get("seq", 0)), dict(e.payload.get("state", {})))
            for e in events
            if e.kind == CHECKPOINT_EVENT_KIND
        ]
        return sorted(found, key=lambda item: item[0])

    # ------------------------------------------------------------------ #
    # Introspection helper (used by tests / diagnostics)
    # ------------------------------------------------------------------ #

    def count(self, execution_id: str) -> int:
        """Number of distinct checkpoints recorded for an execution."""

        eid = _coerce_uuid(execution_id)
        if CHECKPOINTS_AVAILABLE:
            from sqlalchemy import func

            stmt = select(func.count(ExecutionCheckpoint.id)).where(
                ExecutionCheckpoint.execution_id == eid
            )
            return int(self._session.execute(stmt).scalar_one())
        return sum(
            1
            for e in EventRepository(self._session).read_since(eid, 0)
            if e.kind == CHECKPOINT_EVENT_KIND
        )
