"""Execution replay (Master section 61).

Replays a stored execution by feeding its recorded event stream into a fresh
run. The original execution is never mutated (section 61: "Original execution
remains intact"), and the new execution carries ``replay_of`` provenance so the
lineage is recoverable (section 61: "Replay must preserve provenance").

The engine is a thin orchestration layer over the committed
:class:`~uap.runtime.service.ExecutionService`: it reads durable events through
the service's read API and enqueues a new execution through its write API. It
owns no state of its own.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from uap.runtime.service import ExecutionService

__all__ = ["ReplayPlan", "ReplayEngine"]

#: Reserved key carried in a replay execution's inputs (mirrors the service's
#: ``__runtime__`` convention). Holds the provenance back-pointer.
REPLAY_KEY = "__replay__"
#: Service-internal reserved key holding runtime metadata (workflow_ref, ...).
_RUNTIME_KEY = "__runtime__"


class ReplayPlan(BaseModel):
    """A validated replay request (section 61)."""

    model_config = ConfigDict(extra="forbid")

    source_execution_id: str
    #: Replay only events with ``seq >= from_seq`` (0 = the whole run).
    from_seq: int = 0
    #: Input overrides merged into the replayed execution's inputs.
    overrides: dict = Field(default_factory=dict)
    label: str = "replay"


class ReplayEngine:
    """Replays a stored execution into a fresh run with preserved provenance.

    Provenance rules (section 61):

    * The source execution is read-only; replay never writes to it (beyond an
      optional ``EXECUTION_FORKED`` provenance marker).
    * The new execution carries ``replay_of = source_execution_id`` inside its
      ``__replay__`` input block, so the lineage is recoverable.
    * ``overrides`` let a debugger change inputs for the replay without touching
      the recorded original.
    """

    def __init__(self, service: "ExecutionService") -> None:
        self._service = service

    # ------------------------------------------------------------------ #
    # Planning
    # ------------------------------------------------------------------ #

    def build_plan(
        self,
        source_execution_id: str,
        *,
        from_seq: int = 0,
        overrides: dict | None = None,
    ) -> ReplayPlan:
        """Build a :class:`ReplayPlan` for ``source_execution_id``.

        Validates the source exists (reads its events via
        :meth:`ExecutionService.events_since`); an unknown execution raises
        :class:`KeyError`.
        """

        try:
            self._service.status(source_execution_id)
        except LookupError as exc:
            raise KeyError(source_execution_id) from exc

        return ReplayPlan(
            source_execution_id=str(source_execution_id),
            from_seq=int(from_seq),
            overrides=dict(overrides or {}),
        )

    # ------------------------------------------------------------------ #
    # Event-stream reconstruction
    # ------------------------------------------------------------------ #

    def node_results_from_events(
        self,
        source_execution_id: str,
        *,
        upto_seq: int | None = None,
    ) -> dict:
        """Reconstruct ``{node_id: outputs}`` from recorded ``NODE_FINISHED`` events.

        Deterministic: iterates the durable event log in ``seq`` order, keeping
        the last ``NODE_FINISHED`` payload per node. ``payload["outputs"]`` is
        used when present (older events may not carry it, in which case the node
        maps to an empty dict). ``upto_seq`` bounds the reconstruction to the
        state as of that sequence number.
        """

        finished = self._finished_kind()
        results: dict[str, Any] = {}
        for event in self._service.events_since(str(source_execution_id), 0):
            if upto_seq is not None and event["seq"] > upto_seq:
                break
            if event["kind"] != finished:
                continue
            node_id = event.get("node")
            if node_id is None:
                continue
            payload = event.get("payload") or {}
            results[node_id] = (
                dict(payload["outputs"]) if "outputs" in payload else {}
            )
        return results

    # ------------------------------------------------------------------ #
    # Replay creation
    # ------------------------------------------------------------------ #

    def create_replay(self, plan: ReplayPlan) -> str:
        """Enqueue a NEW execution from ``plan`` and return its id.

        The replay reuses the source's ``workflow_ref`` and user inputs, merges
        ``plan.overrides``, and stamps a ``__replay__`` provenance block. When
        the service was constructed with an ``event_sink``, an
        ``EXECUTION_FORKED`` provenance event is recorded on the source
        (best-effort - a sink/vocabulary problem never blocks the replay).
        """

        workflow_ref, user_inputs = self._source_inputs(plan.source_execution_id)
        provenance = {
            "source_execution_id": plan.source_execution_id,
            "from_seq": plan.from_seq,
            "overrides": dict(plan.overrides),
            "replay_of": plan.source_execution_id,
            "label": plan.label,
        }
        inputs: dict[str, Any] = {
            **user_inputs,
            **plan.overrides,
            REPLAY_KEY: provenance,
        }
        new_id = self._service.enqueue(workflow_ref=workflow_ref, inputs=inputs)

        self._record_forked(plan.source_execution_id, new_id)
        return new_id

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _source_inputs(self, source_execution_id: str) -> tuple[str, dict[str, Any]]:
        """Read ``(workflow_ref, user_inputs)`` from the source execution row.

        Uses the service's public ``session_factory`` to read the durable row;
        the ``workflow_ref`` lives in the service-internal ``__runtime__`` block
        and user inputs are everything else.
        """

        from uap.db.repositories import ExecutionRepository

        eid = uuid.UUID(str(source_execution_id))
        with _session_scope(self._service.session_factory) as session:
            row = ExecutionRepository(session).get(eid)
            if row is None:
                raise KeyError(source_execution_id)
            raw = dict(row.input or {})

        meta = raw.get(_RUNTIME_KEY) or {}
        workflow_ref = meta.get("workflow_ref")
        if not workflow_ref:
            raise ValueError(
                f"execution {source_execution_id} has no recorded workflow_ref"
            )
        user_inputs = {k: v for k, v in raw.items() if k != _RUNTIME_KEY}
        return str(workflow_ref), user_inputs

    def _record_forked(self, source_execution_id: str, child_id: str) -> None:
        if getattr(self._service, "_event_sink", None) is None:
            return
        try:
            from uap.events import CanonicalEvent, EventType

            event = CanonicalEvent(
                event_type=EventType.EXECUTION_FORKED,
                execution_id=str(source_execution_id),
                payload={"child_execution_id": str(child_id), "reason": "replay"},
            )
            self._service._event_sink(event)  # best-effort provenance signal
        except Exception:  # noqa: BLE001 - provenance signal must never block
            return

    @staticmethod
    def _finished_kind() -> str:
        try:
            from uap.events import EventType

            return EventType.NODE_FINISHED.value
        except Exception:  # pragma: no cover - events package absent
            return "node_finished"


def _session_scope(session_factory: Any):
    """Thin wrapper around the DB session scope (imported lazily)."""

    from uap.db.engine import session_scope

    return session_scope(session_factory)
