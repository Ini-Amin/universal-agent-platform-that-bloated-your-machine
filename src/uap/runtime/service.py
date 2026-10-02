"""Durable execution service (Master sections 2.3, 21, 37, 38, 39, 45, 46, 73).

The UI connection must **not** own execution lifecycle (section 38). This
service is the durable control plane: it creates executions, records their
canonical events, checkpoints progress, and exposes a read model that a
reconnecting UI/WebSocket resyncs from. A worker (:mod:`uap.runtime.worker`)
drives the actual graph execution.

Design rules encoded here:

* **Everything durable lives in PostgreSQL.** Status transitions, the event log
  and checkpoints are rows. The optional ``event_sink`` is a *fan-out* hook for
  live SSE/WebSocket delivery - losing it never loses execution state
  (section 46: the DB rows are the durable record).
* **Explicit dependency injection** (section 73): ``session_factory``,
  ``graph_resolver`` and ``node_runtime`` are injected. ``uap.execution`` and
  ``uap.events`` are imported lazily so this module imports (and its tests skip
  cleanly) even while those sibling packages are still landing.
* **No hidden singletons**: the service holds no module-level mutable state.

Status-value mapping (the committed ``executions.status`` CHECK constraint only
allows ``pending|running|paused|awaiting_approval|completed|failed|cancelled``
- there is no ``queued`` value, and the constraint is not edited here):

    runtime concept      stored ``executions.status``
    -----------------    ------------------------------
    queued               ``pending``
    running              ``running``
    awaiting_approval    ``awaiting_approval``
    paused               ``paused``
    completed            ``completed``
    failed               ``failed``
    cancelled            ``cancelled``

The remaining runtime-only fields that have no column (``workflow_ref``,
``workspace_id``, cooperative-pause marker, pending approval id, fork origin)
are kept in a reserved ``"__runtime__"`` key inside the execution's ``input``
JSONB document. This is a documented, additive, schema-free extension point; the
user input payload is preserved untouched alongside it and stripped before it is
handed to the executor.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from uap.db.engine import session_scope
from uap.db.models.execution import Execution, ExecutionStatus
from uap.db.repositories import (
    DefinitionRepository,
    EventRepository,
    ExecutionRepository,
)
from uap.db.models.definitions import VersionStatus
from uap.graph import WorkflowGraph

from .checkpoints import CheckpointStore, _jsonable

__all__ = [
    "ExecutionService",
    "RUNTIME_KEY",
    "STATUS_QUEUED",
    "STATUS_MAPPING",
]

#: Reserved key inside ``executions.input`` holding runtime-only metadata.
RUNTIME_KEY = "__runtime__"

#: Runtime "queued" maps onto the committed ``pending`` enum value.
STATUS_QUEUED = ExecutionStatus.PENDING

#: The full runtime-status -> DB-status mapping (documented above).
STATUS_MAPPING: dict[str, ExecutionStatus] = {
    "queued": ExecutionStatus.PENDING,
    "running": ExecutionStatus.RUNNING,
    "awaiting_approval": ExecutionStatus.AWAITING_APPROVAL,
    "paused": ExecutionStatus.PAUSED,
    "completed": ExecutionStatus.COMPLETED,
    "failed": ExecutionStatus.FAILED,
    "cancelled": ExecutionStatus.CANCELLED,
}

#: Node statuses the executor may report that mean "this node will not run again".
_TERMINAL_NODE_STATUSES = frozenset(
    {"finished", "completed", "failed", "skipped", "success", "done"}
)

class _CheckpointingRuntime:
    """Wraps a :class:`NodeRuntime` to checkpoint after every node (section 37).

    The engine calls ``run_node(node, inputs, ctx)``; immediately after a node
    returns, this wrapper snapshots ``ctx.node_results`` / ``ctx.node_status``
    into the durable checkpoint store. That makes the "automatic checkpoint"
    guarantee real: if the worker dies *between* nodes, the last committed node
    is already durably recorded and a fresh worker resumes without re-running it.

    A single instance is safe for concurrent executions because it reads the
    ``execution_id`` from the per-run ``ctx``. Subworkflow executions use a
    synthetic ``parent/node`` id (not a UUID) and are not checkpointed here.
    """

    def __init__(self, inner: Any, session_factory: Any) -> None:
        self._inner = inner
        self._session_factory = session_factory

    async def run_node(self, node: Any, inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
        outputs = await self._inner.run_node(node, inputs, ctx)
        self._checkpoint(node, outputs, ctx)
        return outputs

    def _checkpoint(self, node: Any, outputs: dict[str, Any], ctx: Any) -> None:
        execution_id = str(getattr(ctx, "execution_id", "") or "")
        try:
            uuid.UUID(execution_id)
        except (ValueError, TypeError, AttributeError):
            return  # subworkflow / non-UUID id: not a top-level execution

        results = dict(getattr(ctx, "node_results", {}) or {})
        results[node.id] = dict(outputs or {})
        status = dict(getattr(ctx, "node_status", {}) or {})
        status[node.id] = "completed"
        order = [nid for nid in results]

        with session_scope(self._session_factory) as session:
            store = CheckpointStore(session)
            seq = (store.latest(execution_id) or (0, {}))[0] + 1
            store.save(
                execution_id,
                seq,
                {
                    "status": "running",
                    "node_results": _jsonable(results),
                    "node_status": _jsonable(status),
                    "outputs": {},
                    "order": order,
                    "node_id": node.id,
                },
                node_id=node.id,
            )

def _parse_ref(workflow_ref: str) -> tuple[str, int | None]:
    """Split ``"name@v3"`` into ``("name", 3)``; ``"name"`` -> ``("name", None)``."""

    if "@v" in workflow_ref:
        name, _, raw = workflow_ref.rpartition("@v")
        try:
            return name, int(raw)
        except ValueError:
            return workflow_ref, None
    return workflow_ref, None

class ExecutionService:
    """Durable control plane for workflow executions (sections 21, 37, 38, 46)."""

    def __init__(
        self,
        session_factory: Any,
        *,
        graph_resolver: Callable[[str], WorkflowGraph],
        node_runtime: Any,
        executor: Any | None = None,
        event_sink: Callable[[Any], None] | None = None,
        approval_gate: Any | None = None,
        version_resolver: Callable[[str], uuid.UUID | None] | None = None,
        executor_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._graph_resolver = graph_resolver
        # Wrap the injected runtime so every completed node is checkpointed
        # durably before the next one starts (section 37 "automatic checkpoint").
        self._node_runtime = _CheckpointingRuntime(node_runtime, session_factory)
        self._executor = executor
        self._executor_kwargs = dict(executor_kwargs or {})
        self._event_sink = event_sink
        self._approval_gate = approval_gate
        self._version_resolver = version_resolver

    # ------------------------------------------------------------------ #
    # Small accessors (used by the worker)
    # ------------------------------------------------------------------ #

    @property
    def session_factory(self) -> Any:
        """The injected session factory (workers open their own sessions)."""

        return self._session_factory

    @property
    def graph_resolver(self) -> Callable[[str], WorkflowGraph]:
        return self._graph_resolver

    def _scope(self):
        return session_scope(self._session_factory)

    def _get_executor(self) -> Any:
        """Return the injected executor, lazily building the default one.

        Imported lazily so this module imports even before ``uap.execution``
        exists (section 73: explicit, late-bound dependency).
        """

        if self._executor is None:
            from uap.execution import GraphExecutor  # lazy: sibling wave

            self._executor = GraphExecutor(self._node_runtime, **self._executor_kwargs)
        return self._executor

    # ------------------------------------------------------------------ #
    # Runtime metadata helpers (reserved "__runtime__" input key)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _runtime_meta(row: Execution) -> dict[str, Any]:
        meta = (row.input or {}).get(RUNTIME_KEY)
        return dict(meta) if isinstance(meta, Mapping) else {}

    @staticmethod
    def _user_inputs(row: Execution) -> dict[str, Any]:
        return {
            key: value
            for key, value in dict(row.input or {}).items()
            if key != RUNTIME_KEY
        }

    @staticmethod
    def _write_runtime(
        session: Session, row: Execution, **updates: Any
    ) -> dict[str, Any]:
        meta = ExecutionService._runtime_meta(row)
        for key, value in updates.items():
            if value is None and key in meta and key != "pending_approval":
                continue
            meta[key] = value
        row.input = {**dict(row.input or {}), RUNTIME_KEY: _jsonable(meta)}
        session.flush()
        return meta

    # ------------------------------------------------------------------ #
    # Workflow reference / version resolution
    # ------------------------------------------------------------------ #

    def _resolve_version_id(self, session: Session, workflow_ref: str) -> uuid.UUID:
        """Resolve ``workflow_ref`` to a concrete ``workflow_versions.id``.

        ``executions.workflow_version_id`` is ``NOT NULL`` (committed schema), so
        an execution must be pinned to a real version row. Resolution order:

        1. an injected ``version_resolver`` (when the caller owns the mapping);
        2. ``DefinitionRepository`` lookup by definition name and, when the ref
           is exact (``name@vN``), that exact version, else the latest ACTIVE
           version falling back to the latest version of any status.

        A ref that resolves to no version row raises :class:`LookupError`; the
        documented "NULL allowed" path of the assignment is not possible under
        the committed ``NOT NULL`` constraint.
        """

        if self._version_resolver is not None:
            resolved = self._version_resolver(workflow_ref)
            if resolved is not None:
                return resolved

        name, version = _parse_ref(workflow_ref)
        repo = DefinitionRepository(session)
        definition = repo.get_definition_by_name(name)
        if definition is None:
            raise LookupError(
                f"workflow_ref {workflow_ref!r}: no workflow definition named {name!r}"
            )
        if version is not None:
            row = repo.get_version(definition.id, version)
        else:
            row = repo.latest_version(definition.id, status=VersionStatus.ACTIVE)
            if row is None:
                row = repo.latest_version(definition.id)
        if row is None:
            raise LookupError(
                f"workflow_ref {workflow_ref!r}: no version row found for "
                f"definition {name!r}"
            )
        return row.id

    # ------------------------------------------------------------------ #
    # Write API
    # ------------------------------------------------------------------ #

    def enqueue(
        self,
        *,
        workflow_ref: str,
        inputs: dict[str, Any],
        correlation_id: str | None = None,
        workspace_id: str | None = None,
        requested_by: str | None = None,
    ) -> str:
        """Create a queued execution and record its ``EXECUTION_STARTED`` event.

        Returns the new execution id (a string). The graph is resolved eagerly so
        a bad ``workflow_ref`` fails at enqueue time, not at run time.

        ``requested_by`` attributes the run to a requester identity (audit trail;
        not an authentication boundary — callers can claim any identity).
        """

        # Validate the graph resolves before touching the database.
        self._graph_resolver(workflow_ref)

        with self._scope() as session:
            version_id = self._resolve_version_id(session, workflow_ref)
            corr = uuid.UUID(str(correlation_id)) if correlation_id else None
            row = ExecutionRepository(session).create(
                version_id,
                input=dict(inputs),
                correlation_id=corr,
                status=STATUS_QUEUED,
                requested_by=requested_by,
            )
            meta: dict[str, Any] = {"workflow_ref": workflow_ref}
            if workspace_id is not None:
                meta["workspace_id"] = workspace_id
            row.input = {**dict(inputs), RUNTIME_KEY: meta}
            # Explicit timestamps: ``server_default=func.now()`` is
            # transaction-stable, so two enqueues in one transaction would share
            # a ``created_at`` and lose FIFO ordering. The clock is the single
            # platform clock.
            now = datetime.now(timezone.utc)
            row.created_at = now
            row.updated_at = now
            session.flush()

            EventRepository(session).append(
                row.id,
                self._event_type_value("EXECUTION_STARTED"),
                payload={"workflow_version_id": str(version_id)},
            )
            return str(row.id)

    def pause_request(self, execution_id: str) -> None:
        """Set the cooperative-pause marker checked between nodes (section 37)."""

        eid = uuid.UUID(str(execution_id))
        with self._scope() as session:
            row = ExecutionRepository(session).get(eid)
            if row is None:
                raise LookupError(f"execution {execution_id} not found")
            self._write_runtime(session, row, pause_requested=True)
            EventRepository(session).append(eid, "pause_requested", payload={})

    def resume(self, execution_id: str) -> None:
        """Clear the cooperative-pause marker and re-queue a paused execution."""

        eid = uuid.UUID(str(execution_id))
        with self._scope() as session:
            row = ExecutionRepository(session).get(eid)
            if row is None:
                raise LookupError(f"execution {execution_id} not found")
            self._write_runtime(session, row, pause_requested=False)
            if row.status is ExecutionStatus.PAUSED:
                ExecutionRepository(session).update_status(eid, STATUS_QUEUED)

    def approve_and_resume(
        self,
        execution_id: str,
        approval_id: str,
        approved: bool,
        decided_by: str,
    ) -> None:
        """Resolve a pending approval and re-queue (or cancel) the execution.

        ``approved=True`` clears the pending-approval marker and re-queues the
        execution for a worker; ``approved=False`` cancels it with a reason.
        """

        eid = uuid.UUID(str(execution_id))
        with self._scope() as session:
            row = ExecutionRepository(session).get(eid)
            if row is None:
                raise LookupError(f"execution {execution_id} not found")
            meta = self._runtime_meta(row)
            pending = meta.get("pending_approval") or {}
            recorded_id = pending.get("approval_id") if isinstance(pending, Mapping) else None
            if recorded_id is not None and str(recorded_id) != str(approval_id):
                raise ValueError(
                    f"approval {approval_id!r} does not match pending approval "
                    f"{recorded_id!r} for execution {execution_id}"
                )

            EventRepository(session).append(
                eid,
                self._event_type_value("APPROVAL_DECIDED"),
                payload={
                    "approval_id": str(approval_id),
                    "decision": "granted" if approved else "denied",
                    "decided_by": decided_by,
                },
            )

            # Best-effort: mirror the decision into an injected approval gate so
            # the executor's resume path can see it. The durable record above is
            # authoritative; a gate without this id is tolerated.
            if self._approval_gate is not None:
                try:
                    self._approval_gate.decide(
                        str(approval_id), approved=approved, decided_by=decided_by
                    )
                except Exception:  # noqa: BLE001 - gate state is advisory
                    pass

            if approved:
                meta.pop("pending_approval", None)
                row.input = {**dict(row.input or {}), RUNTIME_KEY: _jsonable(meta)}
                session.flush()
                ExecutionRepository(session).update_status(eid, STATUS_QUEUED)
            else:
                meta.pop("pending_approval", None)
                row.input = {**dict(row.input or {}), RUNTIME_KEY: _jsonable(meta)}
                session.flush()
                ExecutionRepository(session).update_status(
                    eid,
                    ExecutionStatus.CANCELLED,
                    error=f"approval {approval_id} denied by {decided_by}",
                )

    def fork(
        self,
        execution_id: str,
        *,
        from_seq: int | None = None,
        label: str = "fork",
    ) -> str:
        """Fork a new execution from a checkpoint (section 37).

        Copies the checkpoint state up to ``from_seq`` (default: latest) into a
        new execution row with ``resume_count=0`` and a ``forked_from`` /
        ``from_seq`` marker, then records ``EXECUTION_FORKED`` on the source and
        ``EXECUTION_STARTED`` on the child. The source execution is untouched.
        """

        eid = uuid.UUID(str(execution_id))
        with self._scope() as session:
            source = ExecutionRepository(session).get(eid)
            if source is None:
                raise LookupError(f"execution {execution_id} not found")

            checkpoints = CheckpointStore(session).list_for_execution(str(eid))
            if from_seq is None:
                chosen = checkpoints[-1] if checkpoints else None
            else:
                eligible = [cp for cp in checkpoints if cp[0] <= int(from_seq)]
                chosen = eligible[-1] if eligible else None

            seq, state = chosen if chosen is not None else (0, {})
            node_id = state.get("node_id") if isinstance(state, Mapping) else None

            source_meta = self._runtime_meta(source)
            child = ExecutionRepository(session).create(
                source.workflow_version_id,
                input=self._user_inputs(source),
                correlation_id=source.correlation_id,
                status=STATUS_QUEUED,
                requested_by=source.requested_by,
            )
            child_meta: dict[str, Any] = {
                "workflow_ref": source_meta.get("workflow_ref"),
                "forked_from": str(eid),
                "from_seq": int(seq),
                "label": label,
            }
            if source_meta.get("workspace_id") is not None:
                child_meta["workspace_id"] = source_meta["workspace_id"]
            child.input = {
                **self._user_inputs(source),
                RUNTIME_KEY: _jsonable(child_meta),
            }
            now = datetime.now(timezone.utc)
            child.created_at = now
            child.updated_at = now
            session.flush()

            if state:
                CheckpointStore(session).save(
                    str(child.id), int(seq) if seq else 1, state, node_id=node_id
                )

            EventRepository(session).append(
                eid,
                self._event_type_value("EXECUTION_FORKED"),
                payload={
                    "child_execution_id": str(child.id),
                    **({"from_node_id": node_id} if node_id else {}),
                },
            )
            EventRepository(session).append(
                child.id,
                self._event_type_value("EXECUTION_STARTED"),
                payload={"workflow_version_id": str(source.workflow_version_id)},
            )
            return str(child.id)

    # ------------------------------------------------------------------ #
    # Read API (durable read model - a reconnecting UI resyncs from here)
    # ------------------------------------------------------------------ #

    def _resolve_execution_id(self, session: Session, execution_id: str) -> uuid.UUID | None:
        """Resolve a client-facing id to the durable execution row id.

        The UI only ever knows the id it was handed by ``POST /tasks`` — which
        the server stores as ``correlation_id``, NOT as the row's primary key.
        Every read API must therefore accept either form. Without this, the WS
        layer asked for events by correlation id, matched no rows, and fell back
        to the in-memory sink — which holds only the 2 coarse lifecycle events,
        so the Events panel showed 2 rows while PostgreSQL held 23.
        """
        eid = uuid.UUID(str(execution_id))
        repo = ExecutionRepository(session)
        row = repo.get(eid) or repo.get_by_correlation_id(eid)
        return row.id if row is not None else None

    def status(self, execution_id: str) -> dict[str, Any]:
        """Return the durable status projection for ``execution_id``."""

        eid = uuid.UUID(str(execution_id))
        with self._scope() as session:
            row = ExecutionRepository(session).get(eid) or (
                ExecutionRepository(session).get_by_correlation_id(eid)
            )
            if row is None:
                raise LookupError(f"execution {execution_id} not found")
            return {
                "status": row.status.value,
                "resume_count": row.resume_count,
                "error": row.error,
                "output": row.output,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "heartbeat_at": (
                    row.heartbeat_at.isoformat() if row.heartbeat_at else None
                ),
                "requested_by": row.requested_by,
            }

    def prune_executions(self, cutoff: datetime, *, limit: int | None = None) -> int:
        """Delete terminal executions older than ``cutoff``; return the count.

        Thin wrapper over :meth:`ExecutionRepository.prune` so callers never
        touch the repository directly. Retention is opt-in: this runs only
        when someone explicitly calls it (CLI, HTTP endpoint, or operator
        script) — there is no automatic pruning anywhere in the platform.
        """

        with self._scope() as session:
            return ExecutionRepository(session).prune(cutoff, limit=limit)

    def events_since(self, execution_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        """Replay durable events with ``seq > after_seq``, ascending (section 46).

        ``after_seq=0`` returns the whole log; a UI that last saw ``seq=5`` calls
        ``events_since(id, 5)`` to resync with no gaps and no duplicates.
        """

        eid = uuid.UUID(str(execution_id))
        with self._scope() as session:
            resolved = self._resolve_execution_id(session, execution_id)
            if resolved is None:
                return []
            rows = EventRepository(session).read_since(resolved, int(after_seq))
            return [
                {
                    "seq": int(event.seq),
                    "kind": event.kind,
                    "node": event.node,
                    "payload": dict(event.payload or {}),
                    "ts": event.ts.isoformat() if event.ts else None,
                    "id": int(event.id),
                }
                for event in rows
            ]

    def checkpoints(self, execution_id: str) -> list[tuple[int, dict]]:
        """Return ``(seq, state)`` checkpoints for an execution, ascending.

        Accepts either the row id or the client-facing correlation id, matching
        :meth:`status` and :meth:`events_since`.
        """

        with self._scope() as session:
            resolved = self._resolve_execution_id(session, execution_id)
            if resolved is None:
                return []
            return CheckpointStore(session).list_for_execution(str(resolved))

    # ------------------------------------------------------------------ #
    # Execution engine (called by the worker while it holds the lease)
    # ------------------------------------------------------------------ #

    async def run_claimed(self, execution_id: str, worker_id: str) -> str:
        """Execute a claimed execution to completion / pause / requeue.

        Returns the resulting runtime status string. The lease
        (``locked_by`` / ``heartbeat_at``) is released by :meth:`_persist_result`
        on every terminal outcome; the worker's safety net releases it too.
        """

        eid = uuid.UUID(str(execution_id))

        # -- 1. load durable pre-run state (session closed before awaiting) -- #
        with self._scope() as session:
            row = ExecutionRepository(session).get(eid)
            if row is None:
                return "failed"
            meta = self._runtime_meta(row)
            if meta.get("pause_requested"):
                self._write_runtime(session, row, pause_requested=False, pause_honored=True)
                ExecutionRepository(session).update_status(eid, ExecutionStatus.PAUSED)
                EventRepository(session).append(
                    eid,
                    self._event_type_value("EXECUTION_PAUSED"),
                    payload={"reason": "pause_requested"},
                )
                row.locked_by = None
                row.heartbeat_at = datetime.now(timezone.utc)
                session.flush()
                return "paused"
            inputs = self._user_inputs(row)
            workflow_ref = meta.get("workflow_ref")

        graph = self._graph_resolver(workflow_ref)

        # -- 2. resume state from the latest durable checkpoint -------------- #
        with self._scope() as session:
            latest = CheckpointStore(session).latest(str(eid))
        resume_seq, resume_state = latest if latest is not None else (0, None)
        resume_obj = self._build_resume_state(resume_state)

        saw_pause = {"flag": False}

        def _touch_heartbeat() -> None:
            now = datetime.now(timezone.utc)
            with self._scope() as hb_session:
                hb_row = ExecutionRepository(hb_session).get(eid)
                if hb_row is not None:
                    hb_row.heartbeat_at = now
                    hb_row.locked_by = worker_id
                    hb_session.flush()

        def _runtime_sink(*args: Any) -> None:
            _touch_heartbeat()
            with self._scope() as chk_session:
                chk_row = ExecutionRepository(chk_session).get(eid)
                if chk_row is not None and self._runtime_meta(chk_row).get(
                    "pause_requested"
                ):
                    saw_pause["flag"] = True
            self._forward_event(execution_id, args)

        executor = self._get_executor()
        result = await executor.execute(
            graph,
            inputs,
            execution_id=str(eid),
            metadata=meta,
            approval_gate=self._approval_gate,
            event_sink=_runtime_sink,
            resume_state=resume_obj,
        )

        return self._persist_result(
            eid,
            graph,
            result,
            worker_id=worker_id,
            resume_seq=resume_seq,
            saw_pause=saw_pause["flag"],
        )

    def heartbeat(self, execution_id: str, worker_id: str) -> None:
        """Refresh the worker lease (``locked_by`` / ``heartbeat_at``)."""

        eid = uuid.UUID(str(execution_id))
        with self._scope() as session:
            row = ExecutionRepository(session).get(eid)
            if row is None:
                return
            row.locked_by = worker_id
            row.heartbeat_at = datetime.now(timezone.utc)
            session.flush()

    def fail(self, execution_id: str, error: str) -> None:
        """Mark an execution failed and release its lease (worker safety net)."""

        eid = uuid.UUID(str(execution_id))
        with self._scope() as session:
            row = ExecutionRepository(session).get(eid)
            if row is None:
                return
            ExecutionRepository(session).update_status(
                eid, ExecutionStatus.FAILED, error=str(error)
            )
            EventRepository(session).append(
                eid,
                self._event_type_value("EXECUTION_FAILED"),
                payload={"error": str(error)},
            )
            row.locked_by = None
            session.flush()

    def _build_resume_state(self, state: Mapping[str, Any] | None) -> Any | None:
        """Rebuild an ``ExecutionResult`` from a durable checkpoint snapshot.

        The checkpoint stores plain JSON; the engine's ``resume_state`` parameter
        expects an :class:`~uap.execution.ExecutionResult`. Returns ``None`` when
        there is nothing to resume or the sibling package is absent.
        """

        if not state:
            return None
        try:
            from uap.contracts import ApprovalRequest
            from uap.execution import ExecutionResult

            pending = None
            raw_pending = state.get("pending_approval")
            if isinstance(raw_pending, Mapping):
                try:
                    pending = ApprovalRequest.model_validate(raw_pending)
                except Exception:  # pragma: no cover - tolerant of partial data
                    pending = None
            return ExecutionResult(
                status=str(state.get("status") or "paused"),
                outputs=dict(state.get("outputs") or {}),
                node_results={
                    key: dict(value) if isinstance(value, Mapping) else value
                    for key, value in dict(state.get("node_results") or {}).items()
                },
                node_status={
                    key: str(value)
                    for key, value in dict(state.get("node_status") or {}).items()
                },
                error=state.get("error"),
                pending_approval=pending,
                order=list(state.get("order") or []),
            )
        except Exception:  # pragma: no cover - sibling package absent
            return None

    # ------------------------------------------------------------------ #
    # Result persistence
    # ------------------------------------------------------------------ #

    def _persist_result(
        self,
        eid: uuid.UUID,
        graph: WorkflowGraph,
        result: Any,
        *,
        worker_id: str,
        resume_seq: int,
        saw_pause: bool,
    ) -> str:
        status = getattr(result, "status", "failed")
        node_results = getattr(result, "node_results", {}) or {}
        node_status = getattr(result, "node_status", {}) or {}
        outputs = getattr(result, "outputs", {}) or {}
        order = list(getattr(result, "order", []) or [])
        error = getattr(result, "error", None)
        pending = getattr(result, "pending_approval", None)

        node_kind = {node.id: node.kind.value for node in graph.nodes}

        with self._scope() as session:
            row = ExecutionRepository(session).get(eid)
            if row is None:
                return "failed"
            store = CheckpointStore(session)
            events = EventRepository(session)

            # 1. Checkpoint the authoritative snapshot (idempotent upsert).
            seq = (store.latest(str(eid)) or (0, {}))[0] + 1
            last_node = order[-1] if order else None
            pending_payload: dict[str, Any] | None = None
            if pending is not None:
                try:
                    pending_payload = _jsonable(pending.model_dump())
                except Exception:  # pragma: no cover - defensive
                    pending_payload = None
            snapshot = {
                "status": str(status),
                "node_results": _jsonable(node_results),
                "node_status": _jsonable(node_status),
                "outputs": _jsonable(outputs),
                "order": order,
                "error": error,
                "pending_approval": pending_payload,
                "node_id": last_node,
            }
            store.save(str(eid), seq, snapshot, node_id=last_node)

            # 2. Durable node events, derived from the authoritative result.
            existing = {
                (event.kind, event.node)
                for event in events.read_since(eid, 0)
            }
            for node_id in order or list(node_status):
                kind = node_kind.get(node_id, "node")
                if ("node_started", node_id) not in existing:
                    events.append(
                        eid,
                        self._event_type_value("NODE_STARTED"),
                        node=node_id,
                        payload={"node_type": kind},
                    )
                node_state = node_status.get(node_id, "finished")
                if node_state in _TERMINAL_NODE_STATUSES:
                    if node_state == "failed":
                        if ("node_failed", node_id) not in existing:
                            events.append(
                                eid,
                                self._event_type_value("NODE_FAILED"),
                                node=node_id,
                                payload={"error": error or f"node {node_id} failed"},
                            )
                    elif ("node_finished", node_id) not in existing:
                        events.append(
                            eid,
                            self._event_type_value("NODE_FINISHED"),
                            node=node_id,
                            payload={"duration_ms": 0},
                        )

            events.append(
                eid,
                self._event_type_value("CHECKPOINT_SAVED"),
                node=last_node,
                payload={"checkpoint_id": seq, "seq": seq, **({"node_id": last_node} if last_node else {})},
            )

            # 3. Status transition + terminal / pause events.
            repo = ExecutionRepository(session)
            now = datetime.now(timezone.utc)

            if status == "failed" and (
                any(state == "failed" for state in node_status.values()) or not node_status
            ):
                # A node actually failed (or nothing ran): a hard failure.
                repo.update_status(eid, ExecutionStatus.FAILED, error=error)
                events.append(
                    eid,
                    self._event_type_value("EXECUTION_FAILED"),
                    payload={"error": error or "execution failed"},
                )
                final = "failed"
            elif status == "failed":
                # Partial progress interrupted by the step budget / a worker
                # crash: the checkpoint is durable, so this is a resumable pause
                # rather than a hard failure (section 38).
                repo.update_status(eid, ExecutionStatus.PAUSED, output=_jsonable(outputs))
                events.append(
                    eid,
                    self._event_type_value("EXECUTION_PAUSED"),
                    payload={"reason": "interrupted"},
                )
                final = "paused"
            elif saw_pause:
                # A user-requested cooperative pause takes precedence: the user
                # asked to stop, and any approval can be re-raised on resume.
                repo.update_status(eid, ExecutionStatus.PAUSED, output=_jsonable(outputs))
                events.append(
                    eid,
                    self._event_type_value("EXECUTION_PAUSED"),
                    payload={"reason": "cooperative_pause"},
                )
                final = "paused"
            elif pending is not None:
                approval_id = getattr(pending, "approval_id", None) or str(uuid.uuid4())
                action = getattr(pending, "action", "approval")
                self._write_runtime(
                    session,
                    row,
                    pending_approval={
                        "approval_id": str(approval_id),
                        "action": str(action),
                    },
                )
                repo.update_status(
                    eid,
                    ExecutionStatus.AWAITING_APPROVAL,
                    output=_jsonable(outputs),
                )
                events.append(
                    eid,
                    self._event_type_value("APPROVAL_REQUESTED"),
                    payload={"approval_id": str(approval_id), "action": str(action)},
                )
                events.append(
                    eid,
                    self._event_type_value("EXECUTION_PAUSED"),
                    payload={"reason": "awaiting_approval"},
                )
                final = "awaiting_approval"
            elif status == "paused":
                repo.update_status(eid, ExecutionStatus.PAUSED, output=_jsonable(outputs))
                events.append(
                    eid,
                    self._event_type_value("EXECUTION_PAUSED"),
                    payload={"reason": "executor_paused"},
                )
                final = "paused"
            else:
                repo.update_status(eid, ExecutionStatus.COMPLETED, output=_jsonable(outputs))
                events.append(
                    eid,
                    self._event_type_value("EXECUTION_COMPLETED"),
                    payload={"status": "completed"},
                )
                final = "completed"

            # 4. Resume bookkeeping + lease release.
            if resume_seq and final == "completed":
                repo.increment_resume_count(eid)
                events.append(
                    eid,
                    self._event_type_value("EXECUTION_RESUMED"),
                    payload={"resume_count": repo.get(eid).resume_count},
                )

            row.locked_by = None
            row.heartbeat_at = now
            session.flush()
            return final

    # ------------------------------------------------------------------ #
    # Event-type / fan-out helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _event_type_value(name: str) -> str:
        """Return the canonical ``EventType`` value for ``name`` (lazy import)."""

        try:
            from uap.events import EventType

            return EventType[name].value
        except Exception:  # pragma: no cover - events package absent
            return name.lower()

    def _forward_event(self, execution_id: str, args: tuple[Any, ...]) -> None:
        """Best-effort fan-out of an executor event to the optional ``event_sink``.

        The durable record is always written by :meth:`_persist_result`; this
        path only feeds a live SSE/WebSocket sink, so any shape mismatch is
        swallowed rather than breaking the run (section 46).
        """

        if self._event_sink is None:
            return
        try:
            from uap.events import CanonicalEvent, EventType

            event: Any | None = None
            if len(args) == 1 and isinstance(args[0], CanonicalEvent):
                event = args[0]
            elif len(args) >= 2 and isinstance(args[0], str):
                try:
                    event_type = EventType(args[0])
                except ValueError:
                    event_type = None
                if event_type is not None:
                    payload = args[1] if isinstance(args[1], Mapping) else {}
                    event = CanonicalEvent(
                        event_type=event_type,
                        execution_id=str(execution_id),
                        node_id=payload.get("node_id"),
                        payload=dict(payload),
                    )
            if event is not None:
                self._event_sink(event)
        except Exception:  # pragma: no cover - fan-out must never break a run
            return
