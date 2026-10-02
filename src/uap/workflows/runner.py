"""Plain-asyncio workflow runner with checkpoint/resume (Master section 21).

Deterministic orchestrator: nodes are async callables that only reason about a
`WorkflowState`; the runner owns every state transition, the node history, the
retry counter and the checkpoints. No LLM, no IO, no graph framework. LangGraph
replaces this implementation in build Step 6 behind this same interface.

Reserved `state_updates` keys understood by every workflow:

* ``artifacts``    - iterable of `Artifact`, appended to ``state.artifacts``.
* ``output``       - the final answer text, surfaced on `WorkflowResult.output`.
* ``verification`` - a `VerificationResult`; when it does not pass the run ends
                     with status ``failed`` and the verdict's notes as error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from uap.contracts.models import (
    TaskSpec,
    VerificationResult,
    WorkflowResult,
    WorkflowState,
    WorkflowStatus,
    utc_now,
)

Node = Callable[[WorkflowState], Awaitable["NodeResult"]]


@dataclass
class NodeResult:
    """What a node returns: incremental state updates plus the next node name.

    ``next_node=None`` ends the workflow with status ``completed``.
    """

    state_updates: dict[str, Any] = field(default_factory=dict)
    next_node: str | None = None


class StateStore(Protocol):
    """Checkpoint storage (Master section 21). SQLite drops in behind this."""

    def save(self, state: WorkflowState) -> None: ...
    def load(self, task_id: str) -> WorkflowState | None: ...
    def delete(self, task_id: str) -> None: ...


class InMemoryStateStore:
    """Default store: `WorkflowState` JSON keyed by task id."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def save(self, state: WorkflowState) -> None:
        self._store[state.task_id] = state.model_dump_json()

    def load(self, task_id: str) -> WorkflowState | None:
        raw = self._store.get(task_id)
        if raw is None:
            return None
        return WorkflowState.model_validate_json(raw)

    def delete(self, task_id: str) -> None:
        self._store.pop(task_id, None)


class WorkflowRunner:
    """Runs a node graph to completion, checkpointing after every node."""

    def __init__(
        self,
        name: str,
        nodes: dict[str, Node],
        entry: str,
        state_store: StateStore | None = None,
        max_retries: int = 2,
    ) -> None:
        if entry not in nodes:
            raise ValueError(f"entry node {entry!r} is not registered")
        self.name = name
        self.nodes = dict(nodes)
        self.entry = entry
        self.max_retries = max_retries
        self.state_store: StateStore = state_store or InMemoryStateStore()

    # ------------------------------------------------------------------ #
    # Public API
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
        """Continue a checkpointed run from `state.current_node`."""
        state = self.state_store.load(task_id)
        if state is None:
            raise KeyError(f"no checkpoint for task {task_id!r}")
        if state.status in _TERMINAL:
            raise ValueError(f"task {task_id!r} already {state.status.value}")
        state.status = WorkflowStatus.RUNNING
        return await self._execute(state)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _execute(self, state: WorkflowState) -> WorkflowResult:
        error: str | None = None
        while state.current_node is not None:
            node_name = state.current_node
            node = self.nodes[node_name]
            # A repeated entry means a retry or a resume: record the node once.
            if not state.node_history or state.node_history[-1] != node_name:
                state.node_history.append(node_name)
            state.status = WorkflowStatus.RUNNING
            state.updated_at = utc_now()
            try:
                result = await node(state)
            except Exception as exc:  # noqa: BLE001 - node failures are data, not crashes
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
                continue

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

        return self._build_result(state, error=error)

    def _commit(
        self, state: WorkflowState, status: WorkflowStatus | None = None
    ) -> None:
        """Bump the version once and persist the snapshot.

        `status=None` keeps whatever status the caller set: `running` for a
        live checkpoint, `failed` for a terminal one.
        """
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


_TERMINAL = frozenset(
    {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
)
