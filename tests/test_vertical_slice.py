"""§71 vertical slice tests.

Core tests (no database) exercise the runtime adapter, the evaluation gate, the
workspace fallback, the clarification path and ``SliceResult`` serialization.
The full end-to-end run is DB-dependent and skips cleanly when ``uap_test`` is
unreachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from uap.agents.registry import AgentRegistry
from uap.agents.deterministic import EchoAgent, SummarizeAgent
from uap.contracts import TaskSpec
from uap.execution.context import ExecutionContext, NodeExecutionError
from uap.graph import GraphNode, NodeKind, Port, PortType, WorkflowGraph
from uap.slice import (
    PlatformNodeRuntime,
    PlatformSlice,
    SliceEvaluator,
    SliceResult,
    build_research_graph,
)
from uap.tools.registry import ToolRegistry
from uap.workspace.model import Workspace


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ctx(graph: WorkflowGraph) -> ExecutionContext:
    return ExecutionContext(execution_id=str(uuid.uuid4()), graph=graph)


def _agent_node(node_id: str, agent: str) -> GraphNode:
    return GraphNode(
        id=node_id,
        kind=NodeKind.AGENT,
        inputs=[Port(name="value", type=PortType.ANY)],
        outputs=[Port(name="value", type=PortType.ANY)],
        config={"agent": agent},
    )


def _tool_node(node_id: str, tool: str, args: dict | None = None) -> GraphNode:
    config: dict = {"tool": tool}
    if args is not None:
        config["args"] = args
    return GraphNode(
        id=node_id,
        kind=NodeKind.TOOL,
        inputs=[Port(name="value", type=PortType.ANY)],
        outputs=[Port(name="value", type=PortType.ANY)],
        config=config,
    )


def _runtime() -> PlatformNodeRuntime:
    agents = AgentRegistry()
    agents.register(EchoAgent())
    agents.register(SummarizeAgent())
    tools = ToolRegistry()

    async def _echo(**kwargs):
        return kwargs

    from uap.tools.registry import ToolSpec

    tools.register(ToolSpec(name="echo", description="echo", risk_tier=0), _echo)
    return PlatformNodeRuntime(agents, tools, task=TaskSpec(domain="research", goal="g"))


# --------------------------------------------------------------------------- #
# Core: runtime adapter
# --------------------------------------------------------------------------- #


def test_runtime_agent_node_runs_registered_agent() -> None:
    runtime = _runtime()
    graph = build_research_graph()
    node = _agent_node("recon", "echo")
    outputs = asyncio.run(runtime.run_node(node, {"value": "hello"}, _ctx(graph)))
    assert "value" in outputs
    assert outputs["value"]["agent"] == "echo"
    assert "hello" in outputs["value"]["content"]


def test_runtime_tool_node_calls_registered_tool() -> None:
    runtime = _runtime()
    graph = build_research_graph()
    node = _tool_node("fetch", "echo", {"probe": "ok"})
    outputs = asyncio.run(runtime.run_node(node, {"value": "x"}, _ctx(graph)))
    assert outputs["value"]["tool"] == "echo"
    assert outputs["value"]["content"] == {"probe": "ok"}


def test_runtime_missing_agent_raises_node_execution_error() -> None:
    runtime = _runtime()
    graph = build_research_graph()
    node = _agent_node("recon", "does-not-exist")
    with pytest.raises(NodeExecutionError):
        asyncio.run(runtime.run_node(node, {"value": "x"}, _ctx(graph)))


def test_runtime_tool_denial_raises_node_execution_error() -> None:
    # A tier-3 tool with no approval is denied by the default ToolPolicy.
    agents = AgentRegistry()
    tools = ToolRegistry()
    from uap.tools.registry import ToolSpec

    async def _danger(**kwargs):
        return "should-not-run"

    tools.register(
        ToolSpec(name="danger", description="side effect", risk_tier=3), _danger
    )
    runtime = PlatformNodeRuntime(agents, tools)
    graph = build_research_graph()
    node = _tool_node("fetch", "danger", {})
    with pytest.raises(NodeExecutionError):
        asyncio.run(runtime.run_node(node, {"value": "x"}, _ctx(graph)))


def test_runtime_unknown_tool_raises_node_execution_error() -> None:
    runtime = _runtime()
    graph = build_research_graph()
    node = _tool_node("fetch", "nope", {})
    with pytest.raises(NodeExecutionError):
        asyncio.run(runtime.run_node(node, {"value": "x"}, _ctx(graph)))


def test_runtime_deterministic_builtins_return_content() -> None:
    runtime = _runtime()
    graph = build_research_graph()
    syn = GraphNode(
        id="synthesize",
        kind=NodeKind.SYNTHESIS,
        inputs=[Port(name="value")],
        outputs=[Port(name="value")],
    )
    out = asyncio.run(
        runtime.run_node(syn, {"a": {"content": "x"}, "b": {"content": "y"}}, _ctx(graph))
    )
    assert "x" in out["value"]["content"] and "y" in out["value"]["content"]

    evn = GraphNode(
        id="evaluate",
        kind=NodeKind.EVALUATION,
        inputs=[Port(name="value")],
        outputs=[Port(name="value")],
    )
    out = asyncio.run(runtime.run_node(evn, {"a": {"content": "data"}}, _ctx(graph)))
    assert out["value"]["passed"] is True

    kn = GraphNode(
        id="knowledge",
        kind=NodeKind.KNOWLEDGE,
        inputs=[Port(name="value")],
        outputs=[Port(name="value")],
    )
    out = asyncio.run(runtime.run_node(kn, {"a": {"content": "A claim."}}, _ctx(graph)))
    assert out["value"]["statement"] == "A claim."


# --------------------------------------------------------------------------- #
# Core: evaluation gate
# --------------------------------------------------------------------------- #


def test_slice_evaluator_all_criteria_pass() -> None:
    result = SliceEvaluator().evaluate(
        execution_id="e1",
        execution_status="completed",
        node_results={"a": {}},
        artifacts=["art-1"],
        traces=[1],
        knowledge_ids=["k-1"],
    )
    assert result.passed is True
    names = {c.criterion for c in result.criteria}
    assert names == {
        "execution_completed",
        "artifacts_present",
        "trace_coverage",
        "knowledge_eligible",
    }


def test_slice_evaluator_fails_when_incomplete() -> None:
    result = SliceEvaluator().evaluate(
        execution_id="e1",
        execution_status="failed",
        artifacts=[],
        traces=[],
        knowledge_ids=[],
    )
    assert result.passed is False
    failed = {c.criterion for c in result.criteria if not c.passed}
    assert "execution_completed" in failed
    assert "artifacts_present" in failed


# --------------------------------------------------------------------------- #
# Core: graph shape
# --------------------------------------------------------------------------- #


def test_canonical_graph_has_eight_nodes_and_validates() -> None:
    from uap.graph import validate_graph

    graph = build_research_graph()
    # The canonical slice graph is the REAL research pipeline bracketed by
    # INPUT/OUTPUT orchestration nodes: 8 pipeline nodes + 2 = 10.
    pipeline_ids = [n.id for n in graph.nodes if n.config.get("pipeline_node")]
    assert pipeline_ids == [
        "question_analysis",
        "research_planning",
        "fan_out",
        "evidence_extraction",
        "evidence_filtering",
        "cross_verification",
        "synthesis",
        "review",
    ]
    assert len(graph.nodes) == 10
    errors = [i for i in validate_graph(graph) if i.severity == "error"]
    assert errors == [], errors


# --------------------------------------------------------------------------- #
# Core: workspace fallback + clarification (no DB touched)
# --------------------------------------------------------------------------- #


def test_ensure_workspace_defaults_when_no_match(tmp_path: Path) -> None:
    slice_ = PlatformSlice(session_factory=None, artifacts_root=tmp_path)
    # A goal whose vocabulary matches none of the registered workspaces.
    slice_.workspaces = [
        Workspace(id="ws-sec", name="security", description="pentest target bank")
    ]
    task = TaskSpec(domain="research", goal="compare rainfall patterns in deserts")
    workspace, reason = slice_.ensure_workspace(task)
    assert workspace.id == "default"
    assert reason.startswith("no confident match:")


def test_run_clarification_path_returns_early(tmp_path: Path) -> None:
    slice_ = PlatformSlice(session_factory=None, artifacts_root=tmp_path)
    # Empty/short input yields a clarification request from the Entry Workflow,
    # so run() returns before touching the (absent) database.
    result = slice_.run("")
    assert isinstance(result, SliceResult)
    assert result.error is not None
    assert result.error.startswith("clarification required:")
    assert result.execution_id is None


# --------------------------------------------------------------------------- #
# Core: SliceResult JSON round-trip
# --------------------------------------------------------------------------- #


def test_slice_result_json_round_trip() -> None:
    original = SliceResult(
        task_id="t-1",
        workspace_id="default",
        workspace_reason="no confident match: below threshold",
        workflow_ref="research-slice@v1",
        workflow_was_existing=False,
        execution_id="e-1",
        execution_status="completed",
        artifacts=["a-1", "a-2"],
        knowledge_ids=["k-1"],
        evaluation={"verifier": "slice-evaluator", "passed": True},
        git_commit="deadbeef",
        events_emitted=12,
        traces_recorded=1,
        error=None,
    )
    payload = original.model_dump_json()
    restored = SliceResult.model_validate_json(payload)
    assert restored == original


# --------------------------------------------------------------------------- #
# DB-dependent: full end-to-end run
# --------------------------------------------------------------------------- #


DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"


def _resolved_test_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL


def _db_skip_reason() -> str | None:
    try:
        from sqlalchemy import text

        from uap.db import create_db_engine

        engine = create_db_engine(
            _resolved_test_url(), connect_args={"connect_timeout": 2}
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return f"PostgreSQL not reachable at {_resolved_test_url()}: {exc}"
    return None


_SKIP = _db_skip_reason()
if _SKIP is not None:
    print(f"\n[test_vertical_slice] DB tests SKIPPED: {_SKIP}\n")

db_test = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")


@pytest.fixture()
def db_session_factory():
    """A session factory over ``uap_test`` with per-test table isolation.

    The committed schema already lives in ``public`` (alembic-migrated), so a
    dedicated-schema ``create_all`` would be skipped by ``checkfirst`` and every
    write would still land in ``public``. Instead we use ``public`` directly and
    TRUNCATE the tables this slice touches before and after each test, exactly
    like ``test_durable_runtime`` — deterministic and full-suite-safe.
    """
    from sqlalchemy import text

    from uap.db import create_db_engine, create_session_factory

    engine = create_db_engine(_resolved_test_url())
    tables = (
        "knowledge_events",
        "knowledge_provenance",
        "knowledge_items",
        "decision_traces",
        "execution_checkpoints",
        "execution_events",
        "executions",
        "workflow_versions",
        "workflow_definitions",
    )
    statement = text(
        "TRUNCATE "
        + ", ".join(f'"{name}"' for name in tables)
        + " RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(statement)
    try:
        yield create_session_factory(engine)
    finally:
        with engine.begin() as conn:
            conn.execute(statement)
        engine.dispose()


@db_test
def test_full_research_run_completes_with_artifacts(db_session_factory, tmp_path):
    slice_ = PlatformSlice(
        session_factory=db_session_factory, artifacts_root=tmp_path
    )
    result = slice_.run("research and compare two approaches to caching")
    assert result.error is None, result.error
    assert result.execution_status == "completed"
    assert result.workflow_ref == "research-slice@v1"
    assert result.workflow_was_existing is False
    assert result.artifacts, "expected at least one artifact"
    assert result.traces_recorded >= 1
    assert result.events_emitted > 0
    assert result.evaluation.get("verifier") == "slice-evaluator"
    # Artifact files are actually on disk.
    stored = list(Path(tmp_path).rglob("*__v1__*"))
    assert stored, "expected artifact files written to the store root"


@db_test
def test_full_run_proposes_knowledge(db_session_factory, tmp_path):
    slice_ = PlatformSlice(
        session_factory=db_session_factory, artifacts_root=tmp_path
    )
    result = slice_.run("investigate and summarize the tradeoffs of event sourcing")
    assert result.error is None, result.error
    assert result.knowledge_ids, "expected a proposed knowledge item"


@db_test
def test_second_run_reuses_existing_workflow(db_session_factory, tmp_path):
    slice_ = PlatformSlice(
        session_factory=db_session_factory, artifacts_root=tmp_path
    )
    first = slice_.run("research the history of distributed consensus")
    assert first.error is None, first.error
    assert first.workflow_was_existing is False
    # A fresh slice over the SAME database/library should reuse the workflow.
    slice2 = PlatformSlice(
        session_factory=db_session_factory, artifacts_root=tmp_path
    )
    second = slice2.run("research the history of distributed consensus again")
    assert second.error is None, second.error
    assert second.workflow_was_existing is True
