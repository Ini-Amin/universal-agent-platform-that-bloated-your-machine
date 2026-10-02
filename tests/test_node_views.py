"""Node views: make a node's WORK visible on the canvas (canvas-as-stage).

A node may publish a *view* — video, markdown, code, image, iframe, whiteboard —
that the canvas renders. The view is ADDITIONAL output: the node still returns
its normal ``{port: value}`` result, with the published view attached alongside
it. Views are persisted durably and read back through
``GET /api/executions/{id}/views``, which must resolve EITHER the durable row id
OR the client-facing correlation id (the id ``POST /tasks`` hands the UI).

Covers:
1. ``NodeView`` contract: every kind, permissive extras, JSON round-trip.
2. Runtime dispatch: ``config["view"]`` publishes + keeps the normal result.
3. Default synthesis/report view built from the node's real content.
4. ``NodeViewStore`` round-trip (durable, DB).
5. The read API resolves both id forms and returns identical payloads.
6. A full slice run publishes a real view a page reload can still fetch.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from uap.agents.deterministic import EchoAgent, SummarizeAgent
from uap.agents.registry import AgentRegistry
from uap.contracts import NodeView, TaskSpec
from uap.execution.context import ExecutionContext
from uap.graph import GraphNode, NodeKind, Port, PortType, WorkflowGraph
from uap.server.app import create_app
from uap.slice import PlatformNodeRuntime, PlatformSlice, build_research_graph
from uap.tools.registry import ToolRegistry
from uap.views.store import NODE_VIEW_EVENT_KIND, NodeViewStore

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _graph() -> WorkflowGraph:
    return build_research_graph()

def _ctx(graph: WorkflowGraph | None = None, execution_id: str | None = None) -> ExecutionContext:
    return ExecutionContext(
        execution_id=execution_id or str(uuid.uuid4()),
        graph=graph or _graph(),
    )

def _runtime(session_factory=None) -> PlatformNodeRuntime:
    agents = AgentRegistry()
    agents.register(EchoAgent())
    agents.register(SummarizeAgent())
    tools = ToolRegistry()
    return PlatformNodeRuntime(
        agents, tools, task=TaskSpec(domain="research", goal="g"), session_factory=session_factory
    )

def _synth_node(node_id: str = "synthesize") -> GraphNode:
    return GraphNode(
        id=node_id,
        kind=NodeKind.SYNTHESIS,
        inputs=[Port(name="value", type=PortType.ANY)],
        outputs=[Port(name="value", type=PortType.ANY)],
    )

# --------------------------------------------------------------------------- #
# 1. Contract
# --------------------------------------------------------------------------- #

def test_node_view_contract_accepts_every_kind() -> None:
    for kind in (
        "html",
        "markdown",
        "image",
        "video",
        "iframe",
        "code",
        "whiteboard",
        "placeholder",
    ):
        view = NodeView(kind=kind, title="t")
        assert view.kind == kind
        assert view.model_dump()["kind"] == kind

def test_node_view_rejects_unknown_kind() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NodeView(kind="hologram")

def test_node_view_is_permissive_for_renderer_hints() -> None:
    # The UI owns rendering; extra hints (alt/poster/sandbox/html) must survive.
    view = NodeView(
        kind="video",
        title="clip",
        url="https://example.com/a.mp4",
        caption="cap",
        poster="https://example.com/p.png",
    )
    dumped = view.model_dump(exclude_none=True)
    assert dumped["poster"] == "https://example.com/p.png"
    assert NodeView.model_validate_json(view.model_dump_json()) == view

def test_node_view_json_round_trip() -> None:
    view = NodeView(
        kind="code",
        title="snippet",
        text="print('hi')",
        language="python",
    )
    restored = NodeView.model_validate_json(view.model_dump_json())
    assert restored == view
    assert restored.language == "python"

# --------------------------------------------------------------------------- #
# 2-3. Runtime dispatch
# --------------------------------------------------------------------------- #

def test_node_with_view_config_publishes_additional_output() -> None:
    runtime = _runtime()
    node = _synth_node("synthesize")
    node.config = {
        "view": {
            "kind": "markdown",
            "title": "Synthesis",
            "text": "# Result\n\nreal work",
        }
    }
    outputs = asyncio.run(
        runtime.run_node(node, {"value": {"content": "body"}}, _ctx())
    )
    # Normal result is preserved ...
    assert "value" in outputs
    assert "body" in outputs["value"]["content"]
    # ... and the view is ADDITIONAL output, not a replacement.
    assert outputs["value"]["view"]["kind"] == "markdown"
    assert outputs["value"]["view"]["text"] == "# Result\n\nreal work"

def test_synthesis_node_gets_default_markdown_view_from_its_content() -> None:
    runtime = _runtime()
    outputs = asyncio.run(
        runtime.run_node(_synth_node("synthesis"), {"a": {"content": "the report"}}, _ctx())
    )
    view = outputs["value"]["view"]
    assert view["kind"] == "markdown"
    assert "the report" in view["text"]

def test_plain_agent_node_without_view_config_publishes_nothing() -> None:
    runtime = _runtime()
    node = GraphNode(
        id="recon",
        kind=NodeKind.AGENT,
        inputs=[Port(name="value", type=PortType.ANY)],
        outputs=[Port(name="value", type=PortType.ANY)],
        config={"agent": "echo"},
    )
    outputs = asyncio.run(runtime.run_node(node, {"value": "x"}, _ctx()))
    assert "view" not in outputs["value"]

# --------------------------------------------------------------------------- #
# DB-backed fixtures
# --------------------------------------------------------------------------- #

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"

def _resolved_test_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL

def _db_skip_reason() -> str | None:
    try:
        from sqlalchemy import text

        from uap.db import create_db_engine

        engine = create_db_engine(_resolved_test_url(), connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return f"PostgreSQL not reachable at {_resolved_test_url()}: {exc}"
    return None

_SKIP = _db_skip_reason()
db_test = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")

@pytest.fixture()
def db_session_factory():
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
        "TRUNCATE " + ", ".join(f'"{name}"' for name in tables) + " RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(statement)
    try:
        yield create_session_factory(engine)
    finally:
        with engine.begin() as conn:
            conn.execute(statement)
        engine.dispose()

# --------------------------------------------------------------------------- #
# 4. Durable store
# --------------------------------------------------------------------------- #

@db_test
def test_node_view_store_round_trip(db_session_factory) -> None:
    slice_ = PlatformSlice(session_factory=db_session_factory, artifacts_root="/tmp/uap-views-test")
    result = slice_.run("research and compare two approaches to caching")
    assert result.execution_id is not None

    from uap.db.engine import session_scope

    with session_scope(db_session_factory) as session:
        store = NodeViewStore(session)
        store.publish(
            result.execution_id,
            "custom-node",
            {"kind": "code", "title": "c", "text": "x = 1", "language": "python"},
        )
        rows = store.list_for_execution(result.execution_id)
        by_corr = store.list_for_execution(result.task_id)
        assert by_corr == rows

    custom = [r for r in rows if r["node_id"] == "custom-node"]
    assert len(custom) == 1
    assert custom[0]["view"]["kind"] == "code"
    assert custom[0]["view"]["language"] == "python"
    assert custom[0]["created_at"]

# --------------------------------------------------------------------------- #
# 5-6. Read API + full run
# --------------------------------------------------------------------------- #

@db_test
def test_slice_run_publishes_real_synthesis_view(db_session_factory, tmp_path) -> None:
    slice_ = PlatformSlice(session_factory=db_session_factory, artifacts_root=tmp_path)
    result = slice_.run("research and compare two approaches to caching")
    assert result.error is None, result.error
    assert result.execution_status == "completed"

    from uap.db.engine import session_scope

    with session_scope(db_session_factory) as session:
        rows = NodeViewStore(session).list_for_execution(result.execution_id)

    synthesis = [r for r in rows if r["node_id"] == "synthesis"]
    assert synthesis, f"expected a synthesis view, got {rows}"
    view = synthesis[0]["view"]
    assert view["kind"] == "markdown"
    assert view["text"], "the published view must carry the node's real output"

@db_test
def test_views_endpoint_resolves_both_id_forms(db_session_factory, tmp_path) -> None:
    app = create_app(runs_dir=tmp_path, run_inline=True)
    slice_ = PlatformSlice(session_factory=db_session_factory, artifacts_root=tmp_path)
    app.state.slice = slice_

    result = slice_.run("research and compare two approaches to caching")
    assert result.error is None, result.error
    correlation_id = result.task_id
    row_id = result.execution_id
    assert correlation_id != row_id

    client = TestClient(app)
    by_correlation = client.get(f"/api/executions/{correlation_id}/views")
    by_row = client.get(f"/api/executions/{row_id}/views")
    assert by_correlation.status_code == 200, by_correlation.text
    assert by_row.status_code == 200, by_row.text

    assert by_correlation.json() == by_row.json()
    payload = by_correlation.json()
    assert payload, "expected at least one published view"
    assert {"node_id", "view", "created_at"} <= set(payload[0].keys())

@db_test
def test_views_endpoint_unknown_execution_is_404(tmp_path) -> None:
    app = create_app(runs_dir=tmp_path, run_inline=True)
    client = TestClient(app)
    res = client.get(f"/api/executions/{uuid.uuid4()}/views")
    assert res.status_code == 404

def test_node_view_event_kind_is_stable() -> None:
    assert NODE_VIEW_EVENT_KIND == "node_view"
