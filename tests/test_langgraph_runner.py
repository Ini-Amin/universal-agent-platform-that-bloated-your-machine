"""Tests for the LangGraph-backed workflow runner (build Step 6).

These tests require ``langgraph`` and therefore run under
``.venv-langgraph/bin/python``. In the main (Python 3.15) venv they are skipped
cleanly by ``pytest.importorskip`` below.

They prove the swap-compatibility contract: the same ``nodes`` dict produces the
same ``WorkflowResult.status`` and ``node_history`` under both
``WorkflowRunner`` (plain asyncio) and ``LangGraphWorkflowRunner``.
"""

from __future__ import annotations

import builtins
import sqlite3

import pytest

langgraph = pytest.importorskip("langgraph")

from uap.contracts.models import (  # noqa: E402
    ApprovalRequest,
    ApprovalState,
    Domain,
    TaskSpec,
    VerificationResult,
    WorkflowResult,
    WorkflowState,
    WorkflowStatus,
)
from uap.orchestration import LangGraphWorkflowRunner  # noqa: E402
from uap.workflows.runner import (  # noqa: E402
    InMemoryStateStore,
    NodeResult,
    WorkflowRunner,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_task(goal: str = "test") -> TaskSpec:
    return TaskSpec(domain=Domain.RESEARCH, goal=goal, input={"goal": goal})


def linear_nodes(log: list[str]) -> dict:
    """Three-node linear pipeline: a -> b -> c -> END."""

    async def node_a(state: WorkflowState) -> NodeResult:
        log.append("a")
        return NodeResult({"seen_a": True}, next_node="b")

    async def node_b(state: WorkflowState) -> NodeResult:
        log.append("b")
        return NodeResult({"seen_b": True}, next_node="c")

    async def node_c(state: WorkflowState) -> NodeResult:
        log.append("c")
        return NodeResult({"output": "done"}, next_node=None)

    return {"a": node_a, "b": node_b, "c": node_c}


# --------------------------------------------------------------------------- #
# 1. Linear run completes, output set
# --------------------------------------------------------------------------- #


async def test_linear_workflow_completes_and_sets_output():
    log: list[str] = []
    runner = LangGraphWorkflowRunner("linear", linear_nodes(log), entry="a")
    result = await runner.run(make_task())

    assert isinstance(result, WorkflowResult)
    assert result.status is WorkflowStatus.COMPLETED
    assert result.output == "done"
    assert result.workflow == "linear"
    assert result.error is None
    assert log == ["a", "b", "c"]
    await runner.aclose()


# --------------------------------------------------------------------------- #
# 2. node_history order and version == nodes executed + 1
# --------------------------------------------------------------------------- #


async def test_node_history_order_and_version():
    log: list[str] = []
    runner = LangGraphWorkflowRunner("linear", linear_nodes(log), entry="a")
    result = await runner.run(make_task())

    state = runner.state_store.load(result.task_id)
    assert state is not None
    assert state.node_history == ["a", "b", "c"]
    assert state.version == len(state.node_history) + 1
    await runner.aclose()


# --------------------------------------------------------------------------- #
# 3. StateStore checkpoint reflects completion
# --------------------------------------------------------------------------- #


async def test_state_store_checkpoint_after_run():
    log: list[str] = []
    store = InMemoryStateStore()
    runner = LangGraphWorkflowRunner(
        "linear", linear_nodes(log), entry="a", state_store=store
    )
    result = await runner.run(make_task())

    saved = store.load(result.task_id)
    assert saved is not None
    assert saved.status is WorkflowStatus.COMPLETED
    assert saved.current_node is None
    await runner.aclose()


# --------------------------------------------------------------------------- #
# 4. SqliteSaver path: file created and reloadable by a second runner
# --------------------------------------------------------------------------- #


async def test_sqlite_checkpointer_persists_thread_state(tmp_path):
    log: list[str] = []
    cp = tmp_path / "cp.sqlite"
    runner = LangGraphWorkflowRunner(
        "linear", linear_nodes(log), entry="a", checkpointer_path=cp
    )
    result = await runner.run(make_task())
    assert result.status is WorkflowStatus.COMPLETED
    assert cp.exists()
    assert cp.stat().st_size > 0
    await runner.aclose()

    # A second, independent runner instance can read the thread state back.
    log2: list[str] = []
    runner2 = LangGraphWorkflowRunner(
        "linear", linear_nodes(log2), entry="a", checkpointer_path=cp
    )
    restored = await runner2.load_thread_state(result.task_id)
    assert restored is not None
    assert restored.status is WorkflowStatus.COMPLETED
    assert restored.node_history == ["a", "b", "c"]
    await runner2.aclose()

    # The file really is a SQLite database with the checkpoint tables.
    conn = sqlite3.connect(cp)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"checkpoints", "writes"} <= tables


# --------------------------------------------------------------------------- #
# 5. Resume from a mid-pipeline checkpoint does not re-run committed nodes
# --------------------------------------------------------------------------- #


async def test_resume_continues_from_current_node_without_duplicates():
    log: list[str] = []
    store = InMemoryStateStore()
    task = make_task()

    # Craft a checkpoint as if a process died after nodes a and b committed.
    mid = WorkflowState(
        task_id=task.task_id,
        workflow="linear",
        status=WorkflowStatus.CHECKPOINTED,
        current_node="c",
        node_history=["a", "b"],
        version=3,
        data={"task": task.model_dump()},
    )
    store.save(mid)

    runner = LangGraphWorkflowRunner(
        "linear", linear_nodes(log), entry="a", state_store=store
    )
    result = await runner.resume(task.task_id)

    assert result.status is WorkflowStatus.COMPLETED
    assert log == ["c"]  # only the remaining node ran
    state = store.load(task.task_id)
    assert state is not None
    assert state.node_history == ["a", "b", "c"]  # no duplicates
    assert state.version == 4
    await runner.aclose()


# --------------------------------------------------------------------------- #
# 6. Node raising is retried max_retries then fails with the error surfaced
# --------------------------------------------------------------------------- #


async def test_failing_node_retried_then_failed():
    attempts = {"n": 0}

    async def boom(state: WorkflowState) -> NodeResult:
        attempts["n"] += 1
        raise ValueError("kaboom")

    runner = LangGraphWorkflowRunner(
        "failing", {"boom": boom}, entry="boom", max_retries=2
    )
    result = await runner.run(make_task())

    assert result.status is WorkflowStatus.FAILED
    assert attempts["n"] == 3  # initial + 2 retries
    assert result.error is not None and "kaboom" in result.error
    state = runner.state_store.load(result.task_id)
    assert state is not None
    assert state.retries == 3
    assert "kaboom" in state.data["error"]
    await runner.aclose()


# --------------------------------------------------------------------------- #
# 7. Conditional branch: next_node="b" vs None
# --------------------------------------------------------------------------- #


async def test_conditional_branch_routes_to_b():
    log: list[str] = []

    async def start(state: WorkflowState) -> NodeResult:
        log.append("start")
        return NodeResult({}, next_node="b")

    async def b(state: WorkflowState) -> NodeResult:
        log.append("b")
        return NodeResult({"output": "went-to-b"}, next_node=None)

    runner = LangGraphWorkflowRunner(
        "branch", {"start": start, "b": b}, entry="start"
    )
    result = await runner.run(make_task())

    assert log == ["start", "b"]
    assert result.status is WorkflowStatus.COMPLETED
    assert result.output == "went-to-b"
    await runner.aclose()


async def test_conditional_branch_ends_on_none():
    log: list[str] = []

    async def start(state: WorkflowState) -> NodeResult:
        log.append("start")
        return NodeResult({"output": "ended"}, next_node=None)

    async def b(state: WorkflowState) -> NodeResult:
        log.append("b")
        return NodeResult({"output": "should-not-run"}, next_node=None)

    runner = LangGraphWorkflowRunner(
        "branch", {"start": start, "b": b}, entry="start"
    )
    result = await runner.run(make_task())

    assert log == ["start"]
    assert result.status is WorkflowStatus.COMPLETED
    assert result.output == "ended"
    await runner.aclose()


# --------------------------------------------------------------------------- #
# 8. Approval pause and resume
# --------------------------------------------------------------------------- #


async def test_pause_for_approval_then_resume_approved():
    log: list[str] = []

    async def publish(state: WorkflowState) -> NodeResult:
        log.append("publish")
        return NodeResult({"output": "published"}, next_node=None)

    store = InMemoryStateStore()
    runner = LangGraphWorkflowRunner(
        "approval", {"publish": publish}, entry="publish", state_store=store
    )
    task = make_task()

    # The workflow stopped just before the side-effectful node.
    store.save(
        WorkflowState(
            task_id=task.task_id,
            workflow="approval",
            status=WorkflowStatus.CHECKPOINTED,
            current_node="publish",
            node_history=["draft"],
            version=2,
        )
    )

    approval = ApprovalRequest(task_id=task.task_id, action="publish")
    paused = await runner.pause_for_approval(task.task_id, approval)

    assert paused.status is WorkflowStatus.AWAITING_APPROVAL
    assert paused.pending_approval is not None
    assert paused.pending_approval.state is ApprovalState.PENDING_APPROVAL
    saved = store.load(task.task_id)
    assert saved is not None and saved.status is WorkflowStatus.AWAITING_APPROVAL
    assert log == []  # nothing ran while paused

    resumed = await runner.resume_with_decision(task.task_id, True, "alice")
    assert resumed.status is WorkflowStatus.COMPLETED
    assert resumed.output == "published"
    assert log == ["publish"]  # the gated node ran only after approval
    final = store.load(task.task_id)
    assert final is not None
    assert final.pending_approval is not None
    assert final.pending_approval.state is ApprovalState.APPROVED
    assert final.pending_approval.decided_by == "alice"
    await runner.aclose()


async def test_resume_with_rejection_fails():
    async def first(state: WorkflowState) -> NodeResult:
        return NodeResult({"output": "ready"}, next_node=None)

    store = InMemoryStateStore()
    runner = LangGraphWorkflowRunner(
        "approval", {"first": first}, entry="first", state_store=store
    )
    task = make_task()
    await runner.run(task)
    await runner.pause_for_approval(
        task.task_id, ApprovalRequest(task_id=task.task_id, action="publish")
    )

    result = await runner.resume_with_decision(task.task_id, False, "bob")
    assert result.status is WorkflowStatus.FAILED
    assert result.error is not None and "bob" in result.error
    await runner.aclose()


async def test_native_interrupt_maps_to_awaiting_approval():
    """LangGraph `interrupt()` inside a node maps onto the UAP approval states.

    The underlying mechanism is `interrupt(value)` + `Command(resume=...)`; the
    runner detects the pause and exposes it through the same
    ``pending_approval`` / ``AWAITING_APPROVAL`` contract.
    """
    from langgraph.types import interrupt

    async def draft(state: WorkflowState) -> NodeResult:
        return NodeResult({"draft": "x"}, next_node="gate")

    async def gate(state: WorkflowState) -> NodeResult:
        decision = interrupt({"action": "publish"})
        return NodeResult({"output": f"published:{decision['approved']}"}, next_node=None)

    store = InMemoryStateStore()
    runner = LangGraphWorkflowRunner(
        "native", {"draft": draft, "gate": gate}, entry="draft", state_store=store
    )
    task = make_task()

    paused = await runner.run(task)
    assert paused.status is WorkflowStatus.AWAITING_APPROVAL
    state = store.load(task.task_id)
    assert state is not None
    assert state.pending_approval is not None
    assert state.pending_approval.action == "publish"

    resumed = await runner.resume_with_decision(task.task_id, True, "carol")
    assert resumed.status is WorkflowStatus.COMPLETED
    assert resumed.output == "published:True"
    await runner.aclose()


# --------------------------------------------------------------------------- #
# 9. Swap compatibility: identical status and node_history under both runners
# --------------------------------------------------------------------------- #


async def test_same_nodes_swap_compatible_with_asyncio_runner():
    task = make_task()

    log_a: list[str] = []
    asyncio_runner = WorkflowRunner("linear", linear_nodes(log_a), entry="a")
    asyncio_result = await asyncio_runner.run(task)

    log_b: list[str] = []
    lg_runner = LangGraphWorkflowRunner("linear", linear_nodes(log_b), entry="a")
    lg_result = await lg_runner.run(task)

    assert asyncio_result.status == lg_result.status
    assert asyncio_result.output == lg_result.output

    asyncio_state = asyncio_runner.state_store.load(task.task_id)
    lg_state = lg_runner.state_store.load(task.task_id)
    assert asyncio_state is not None and lg_state is not None
    assert asyncio_state.node_history == lg_state.node_history
    assert log_a == log_b
    await lg_runner.aclose()


async def test_swap_compatibility_with_verification_failure():
    """A non-passing verification fails identically under both orchestrators."""

    def nodes() -> dict:
        async def a(state: WorkflowState) -> NodeResult:
            return NodeResult(
                {"verification": VerificationResult(verifier="v", passed=False, notes="nope")},
                next_node=None,
            )

        return {"a": a}

    task = make_task()
    asyncio_result = await WorkflowRunner("v", nodes(), entry="a").run(task)
    lg_runner = LangGraphWorkflowRunner("v", nodes(), entry="a")
    lg_result = await lg_runner.run(task)

    assert asyncio_result.status is WorkflowStatus.FAILED
    assert asyncio_result.status == lg_result.status
    assert asyncio_result.error == lg_result.error
    await lg_runner.aclose()


# --------------------------------------------------------------------------- #
# 10. Lazy import: module imports without langgraph; constructor raises clearly
# --------------------------------------------------------------------------- #


def test_module_imports_without_langgraph_available(monkeypatch):
    """The module must import with no top-level langgraph import."""
    import importlib

    import uap.orchestration.langgraph_runner as mod

    # A top-level import would already have failed above; assert the contract
    # explicitly: the module source has no module-scope langgraph import.
    source = importlib.util.find_spec("uap.orchestration.langgraph_runner")
    assert source is not None
    text = open(mod.__file__, encoding="utf-8").read()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import langgraph", "from langgraph")):
            # Only allowed inside the lazy import block (indented).
            assert line.startswith(" "), f"top-level langgraph import: {line!r}"

    # Simulate langgraph being absent and assert the clear RuntimeError.
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "langgraph" or name.startswith("langgraph."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(RuntimeError, match="LangGraph is required: use .venv-langgraph"):
        LangGraphWorkflowRunner("x", {"a": _noop_node}, entry="a")


async def _noop_node(state: WorkflowState) -> NodeResult:  # pragma: no cover
    return NodeResult({}, next_node=None)
