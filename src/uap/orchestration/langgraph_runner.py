"""LangGraph-backed workflow runner (Master sections 14, 20, 21, 24, 26, 29).

`LangGraphWorkflowRunner` is the drop-in equivalent of
:class:`uap.workflows.runner.WorkflowRunner`: same constructor shape, same
``run`` / ``resume`` behaviour, same ``NodeResult`` / ``StateStore`` contracts,
same status transitions and the same ``WorkflowResult`` shape. Domain workflows
hand it a ``dict[str, Node]`` and never learn which orchestrator is driving them
(Master section 29 rule 20: LangGraph is orchestration, not the architecture).

Design notes
------------
* **Lazy import.** Nothing from ``langgraph`` is imported at module import time,
  so this module imports cleanly in the main (Python 3.15) venv. The import
  happens in ``__init__``; when it fails the constructor raises
  ``RuntimeError("LangGraph is required: use .venv-langgraph")``.
* **StateGraph state is a dict** mirroring every ``WorkflowState`` field (plus a
  private ``error`` slot). Each graph node is a thin wrapper that rebuilds the
  ``WorkflowState`` from the channel values, runs the user's async callable,
  applies ``NodeResult.state_updates``, appends to ``node_history``, bumps
  ``version`` and checkpoints through both the ``StateStore`` and the LangGraph
  checkpointer.
* **Routing.** ``next_node=None`` routes to ``END``; otherwise a conditional
  edge routes to the named node. Retries are modelled as a self-loop back to the
  failing node, so a node that raises is retried ``max_retries`` times and then
  fails with the error surfaced in ``data["error"]`` -- exactly like the
  asyncio runner.
* **Checkpointing.** ``thread_id`` is ``task.task_id``. With
  ``checkpointer_path`` the runner uses ``AsyncSqliteSaver`` (async-safe in
  LangGraph 1.2); without it, ``MemorySaver``. Re-invoking the same thread
  continues from the last checkpoint; ``resume`` never re-runs a committed node.
* **Human approval (Master section 20).** ``pause_for_approval`` /
  ``resume_with_decision`` own the ``AWAITING_APPROVAL -> APPROVED/REJECTED``
  transition. Under the hood the mechanism LangGraph exposes for this is
  ``langgraph.types.interrupt()`` inside a node plus
  ``graph.invoke(Command(resume=...))`` on the same ``thread_id`` (requires a
  checkpointer). This runner also *detects* a genuine ``interrupt()`` pause in
  ``run`` and maps it to ``AWAITING_APPROVAL``, then resumes it with
  ``Command(resume={"approved": ..., "decided_by": ...})``. The explicit
  state-store API is the supported contract; the interrupt path is the same
  transition implemented with LangGraph's native primitive.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, TypedDict

from uap.contracts.models import (
    ApprovalRequest,
    ApprovalState,
    TaskSpec,
    VerificationResult,
    WorkflowResult,
    WorkflowState,
    WorkflowStatus,
    utc_now,
)
from uap.workflows.runner import (
    InMemoryStateStore,
    NodeResult,
    StateStore,
)

__all__ = ["LangGraphWorkflowRunner"]

Node = Callable[[WorkflowState], Awaitable[NodeResult]]


class _GraphState(TypedDict, total=False):
    """LangGraph channel schema: a dict mirror of `WorkflowState`.

    ``error`` is a runner-owned slot (the terminal error message that the
    asyncio runner keeps in a local variable); it is not part of the public
    contract.
    """

    task_id: str
    workflow: str
    status: str
    current_node: str | None
    node_history: list[str]
    retries: int
    pending_approval: dict[str, Any] | None
    artifacts: list[Any]
    data: dict[str, Any]
    version: int
    updated_at: str
    error: str | None


_TERMINAL = frozenset(
    {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
)

_MISSING_MESSAGE = "LangGraph is required: use .venv-langgraph"


class LangGraphWorkflowRunner:
    """Runs a node graph to completion under LangGraph, checkpointing per node."""

    def __init__(
        self,
        name: str,
        nodes: dict[str, Node],
        entry: str,
        state_store: StateStore | None = None,
        max_retries: int = 2,
        checkpointer_path: str | Path | None = None,
    ) -> None:
        if entry not in nodes:
            raise ValueError(f"entry node {entry!r} is not registered")

        # LAZY import: the module must import cleanly without langgraph installed.
        try:
            from langgraph.checkpoint.memory import MemorySaver
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            from langgraph.errors import GraphInterrupt
            from langgraph.graph import END, START, StateGraph
            from langgraph.types import Command, interrupt
        except ImportError as exc:  # pragma: no cover - depends on the venv
            raise RuntimeError(_MISSING_MESSAGE) from exc

        self._StateGraph = StateGraph
        self._START = START
        self._END = END
        self._MemorySaver = MemorySaver
        self._AsyncSqliteSaver = AsyncSqliteSaver
        self._GraphInterrupt = GraphInterrupt
        self._Command = Command
        self._interrupt = interrupt

        self.name = name
        self.nodes = dict(nodes)
        self.entry = entry
        self.max_retries = max_retries
        self.state_store: StateStore = state_store or InMemoryStateStore()
        self.checkpointer_path = (
            Path(checkpointer_path) if checkpointer_path is not None else None
        )

        self._saver: Any = None
        self._saver_cm: Any = None
        self._graph: Any = None
        self._builder = self._build_builder()

    # ------------------------------------------------------------------ #
    # Public API (mirrors WorkflowRunner)
    # ------------------------------------------------------------------ #

    async def run(self, task: TaskSpec) -> WorkflowResult:
        """Start a fresh run from the entry node."""
        state = WorkflowState(
            task_id=task.task_id,
            workflow=self.name,
            status=WorkflowStatus.RUNNING,
            current_node=self.entry,
            data={"task": task.model_dump()},
        )
        self.state_store.save(state)
        return await self._execute(state)

    async def resume(self, task_id: str) -> WorkflowResult:
        """Continue a checkpointed run from `state.current_node`.

        Loads from the ``StateStore`` when it has the task, otherwise from the
        LangGraph checkpoint for ``thread_id == task_id``. Committed nodes are
        never re-run: routing resumes at ``current_node``.
        """
        state = await self._load_state(task_id)
        if state is None:
            raise KeyError(f"no checkpoint for task {task_id!r}")
        if state.status in _TERMINAL:
            raise ValueError(f"task {task_id!r} already {state.status.value}")
        state.status = WorkflowStatus.RUNNING
        return await self._execute(state)

    # ------------------------------------------------------------------ #
    # Human approval (Master section 20)
    # ------------------------------------------------------------------ #

    async def pause_for_approval(
        self, task_id: str, approval: ApprovalRequest
    ) -> WorkflowState:
        """Pause a run at an explicit approval point.

        Stores ``approval`` in ``state.pending_approval`` and moves the state to
        ``AWAITING_APPROVAL``. The resume point (``current_node``) is left
        untouched so ``resume_with_decision`` continues exactly where the
        workflow stopped. The same snapshot is written to the LangGraph
        checkpoint, which is where a native ``interrupt()`` would pause.
        """
        state = await self._load_state(task_id)
        if state is None:
            raise KeyError(f"no checkpoint for task {task_id!r}")
        state.pending_approval = approval
        state.status = WorkflowStatus.AWAITING_APPROVAL
        state.version += 1
        state.updated_at = utc_now()
        self.state_store.save(state)
        graph = await self._ensure_graph()
        await graph.aupdate_state(
            self._config(task_id), self._to_graph(state), as_node=self._START
        )
        return state

    async def resume_with_decision(
        self, task_id: str, approved: bool, decided_by: str
    ) -> WorkflowResult:
        """Resolve a paused approval and continue (or terminate) the run.

        ``approved=False`` is terminal: the run ends ``failed`` with the
        decision recorded. ``approved=True`` clears the pause and continues
        from ``current_node``. When the pause came from a native
        ``interrupt()`` node, the run is continued with
        ``Command(resume={"approved": ..., "decided_by": ...})``.
        """
        state = await self._load_state(task_id)
        if state is None:
            raise KeyError(f"no checkpoint for task {task_id!r}")
        if state.pending_approval is None:
            raise ValueError(f"task {task_id!r} has no pending approval")

        decision = state.pending_approval.model_copy(
            update={
                "state": (
                    ApprovalState.APPROVED if approved else ApprovalState.REJECTED
                ),
                "decided_at": utc_now(),
                "decided_by": decided_by,
            }
        )
        state.pending_approval = decision

        if not approved:
            state.status = WorkflowStatus.FAILED
            state.current_node = None
            state.version += 1
            state.updated_at = utc_now()
            self.state_store.save(state)
            return self._build_result(
                state, error=f"approval rejected by {decided_by}"
            )

        graph = await self._ensure_graph()
        snapshot = await graph.aget_state(self._config(task_id))
        if snapshot.interrupts:
            # Native LangGraph pause: continue it with Command(resume=...).
            self.state_store.save(state)
            result = await graph.ainvoke(
                self._Command(resume={"approved": approved, "decided_by": decided_by}),
                self._config(task_id),
            )
            final = self._state_from_graph(result)
            final.pending_approval = decision
            self.state_store.save(final)
            return self._build_result(final, error=result.get("error"))

        if state.current_node is None:
            # The approval was the last gate: nothing left to execute.
            state.status = WorkflowStatus.COMPLETED
            state.version += 1
            state.updated_at = utc_now()
            self.state_store.save(state)
            return self._build_result(state)

        state.status = WorkflowStatus.RUNNING
        return await self._execute(state)

    # ------------------------------------------------------------------ #
    # Checkpoint / graph lifecycle
    # ------------------------------------------------------------------ #

    @property
    def graph(self) -> Any:
        """The compiled LangGraph, or None before the first run/resume."""
        return self._graph

    async def load_thread_state(self, task_id: str) -> WorkflowState | None:
        """Read a ``WorkflowState`` back out of the LangGraph checkpoint."""
        graph = await self._ensure_graph()
        snapshot = await graph.aget_state(self._config(task_id))
        if not snapshot.values:
            return None
        return self._state_from_graph(snapshot.values)

    async def aclose(self) -> None:
        """Release the SQLite connection held by the async checkpointer."""
        if self._saver_cm is not None:
            await self._saver_cm.__aexit__(None, None, None)
        self._saver_cm = None
        self._saver = None
        self._graph = None

    async def _ensure_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        if self.checkpointer_path is not None:
            self._saver_cm = self._AsyncSqliteSaver.from_conn_string(
                str(self.checkpointer_path)
            )
            self._saver = await self._saver_cm.__aenter__()
        else:
            self._saver = self._MemorySaver()
        self._graph = self._builder.compile(checkpointer=self._saver)
        return self._graph

    # ------------------------------------------------------------------ #
    # Graph construction
    # ------------------------------------------------------------------ #

    def _build_builder(self) -> Any:
        builder = self._StateGraph(_GraphState)
        for node_name, node in self.nodes.items():
            builder.add_node(node_name, self._make_node(node_name, node))
            builder.add_conditional_edges(node_name, self._route)
        # Entry routing reads `current_node`, so START is dispatched the same way
        # a resume is: the seed state decides which node runs first.
        builder.add_conditional_edges(self._START, self._route)
        return builder

    def _route(self, graph_state: _GraphState) -> str:
        """Conditional edge: `current_node` or END when it is None/empty."""
        return graph_state.get("current_node") or self._END

    def _make_node(self, node_name: str, node: Node) -> Callable[[_GraphState], Any]:
        """Wrap a user node as a LangGraph node function."""

        async def _graph_node(graph_state: _GraphState) -> _GraphState:
            state = self._state_from_graph(graph_state)
            error = graph_state.get("error")

            # A repeated entry means a retry or a resume: record the node once.
            if not state.node_history or state.node_history[-1] != node_name:
                state.node_history.append(node_name)
            state.status = WorkflowStatus.RUNNING
            state.updated_at = utc_now()

            try:
                result = await node(state)
            except self._GraphInterrupt:
                # A native interrupt() pause must bubble up untouched.
                raise
            except Exception as exc:  # noqa: BLE001 - node failures are data
                state.retries += 1
                state.data["error"] = f"{node_name}: {type(exc).__name__}: {exc}"
                if state.retries > self.max_retries:
                    state.status = WorkflowStatus.FAILED
                    state.current_node = None
                    error = (
                        f"node {node_name!r} failed after "
                        f"{state.retries} attempts: {exc}"
                    )
                    self._commit(state)
                else:
                    self._commit(state, status=WorkflowStatus.CHECKPOINTED)
                    state.current_node = node_name  # self-loop retry
                return self._to_graph(state, error=error)

            self._apply(state, result.state_updates)
            state.current_node = result.next_node

            verification = self._verification(state)
            if verification is not None and not verification.passed:
                state.status = WorkflowStatus.FAILED
                state.current_node = None
                error = f"verification failed: {verification.notes}"
                self._commit(state)
            elif state.current_node is None:
                self._commit(state, status=WorkflowStatus.COMPLETED)
            else:
                self._commit(state, status=WorkflowStatus.CHECKPOINTED)
            return self._to_graph(state, error=error)

        return _graph_node

    # ------------------------------------------------------------------ #
    # Internals (mirror WorkflowRunner)
    # ------------------------------------------------------------------ #

    async def _execute(self, state: WorkflowState) -> WorkflowResult:
        graph = await self._ensure_graph()
        config = self._config(state.task_id)
        # Seed/overwrite the thread with the authoritative UAP state, then run.
        await graph.aupdate_state(config, self._to_graph(state), as_node=self._START)
        result_state = await graph.ainvoke(None, config)

        snapshot = await graph.aget_state(config)
        if snapshot.interrupts:
            final = self._state_from_graph(snapshot.values)
            final.status = WorkflowStatus.AWAITING_APPROVAL
            final.pending_approval = self._approval_from_interrupts(
                snapshot.interrupts, final.task_id
            )
            final.version += 1
            final.updated_at = utc_now()
            self.state_store.save(final)
            return self._build_result(final, error=None)

        final = self._state_from_graph(result_state)
        return self._build_result(final, error=result_state.get("error"))

    async def _load_state(self, task_id: str) -> WorkflowState | None:
        state = self.state_store.load(task_id)
        if state is not None:
            return state
        return await self.load_thread_state(task_id)

    def _commit(
        self, state: WorkflowState, status: WorkflowStatus | None = None
    ) -> None:
        """Bump the version once and persist the snapshot (same as asyncio)."""
        if status is not None:
            state.status = status
        state.version += 1
        state.updated_at = utc_now()
        self.state_store.save(state)

    @staticmethod
    def _apply(state: WorkflowState, updates: dict[str, Any]) -> None:
        for key, value in updates.items():
            if key == "artifacts":
                state.artifacts.extend(value)
            else:
                state.data[key] = value

    @staticmethod
    def _verification(state: WorkflowState) -> VerificationResult | None:
        value = state.data.get("verification")
        if isinstance(value, dict):  # checkpoint JSON round-trip
            return VerificationResult(**value)
        return value

    def _build_result(
        self, state: WorkflowState, error: str | None = None
    ) -> WorkflowResult:
        return WorkflowResult(
            task_id=state.task_id,
            workflow=state.workflow,
            status=state.status,
            output=state.data.get("output", ""),
            artifacts=list(state.artifacts),
            verification=self._verification(state),
            error=error,
        )

    # ------------------------------------------------------------------ #
    # State <-> graph channel conversion
    # ------------------------------------------------------------------ #

    @staticmethod
    def _config(task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": task_id}}

    @staticmethod
    def _to_graph(state: WorkflowState, error: str | None = None) -> dict[str, Any]:
        """`WorkflowState` -> JSON-native channel dict (checkpoint friendly)."""
        payload: dict[str, Any] = state.model_dump(mode="json")
        payload["error"] = error
        return payload

    @staticmethod
    def _state_from_graph(graph_state: dict[str, Any]) -> WorkflowState:
        """Channel dict -> `WorkflowState`, ignoring runner-private keys."""
        payload = {
            key: value
            for key, value in graph_state.items()
            if key in WorkflowState.model_fields
        }
        return WorkflowState.model_validate(payload)

    @staticmethod
    def _approval_from_interrupts(
        interrupts: Any, task_id: str
    ) -> ApprovalRequest | None:
        """Best-effort mapping of a native interrupt payload to `ApprovalRequest`."""
        if not interrupts:
            return None
        payload = getattr(interrupts[0], "value", None)
        if isinstance(payload, ApprovalRequest):
            return payload
        if isinstance(payload, dict):
            try:
                return ApprovalRequest(task_id=task_id, **payload)
            except Exception:  # noqa: BLE001 - arbitrary interrupt payloads
                pass
        return ApprovalRequest(task_id=task_id, action=str(payload))
