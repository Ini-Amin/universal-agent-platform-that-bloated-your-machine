"""Tests for the Learning Domain and Workflow (Master section 7 / Astra UX design).

Verifies:
1. Goal analysis parses topic, progression (scratch -> intermediate), and prior knowledge.
2. Curriculum planning generates ordered modules with rough time estimates and finish lines.
3. Teaching material is emitted as canvas views in Astra's prescribed hierarchy:
   - Exercise first (kind: "code", ready-to-start with clear finish line)
   - Explanation second (kind: "markdown")
   - Summary third (kind: "markdown")
   - Sources fourth (kind: "markdown", with real URLs and honesty banner when simulated)
4. Video view is omitted when no verified real video URL exists (no fabricated links).
5. Honesty requirement: when source collectors are stubs, the simulation banner is present.
   When sources are real, the simulation banner is absent.
6. Lesson artifact `lesson.md` and `curriculum.json` are produced.
7. Verification criteria evaluate deterministically.
8. API integration: POST /tasks routes `learn ...` to the learning workflow,
   and GET /api/executions/{id}/views returns the published views.
"""

from __future__ import annotations

import asyncio
import json
import pytest
from fastapi.testclient import TestClient

from uap.contracts.models import (
    Domain,
    NodeView,
    TaskSpec,
    WorkflowStatus,
)
from uap.models.router import EXECUTABLE_DOMAINS
from uap.server.app import create_app
from uap.templates import catalog, get_template
from uap.workflows.learning import (
    HONESTY_BANNER,
    WORKFLOW_NAME,
    LearningWorkflow,
    _analyze_goal,
    _build_backend_curriculum,
)
from uap.workflows.research import _item


# --------------------------------------------------------------------------- #
# 1. Goal Analysis & Curriculum Planning
# --------------------------------------------------------------------------- #


def test_goal_analysis_extracts_topic_and_level() -> None:
    analysis = _analyze_goal("learn backend development from scratch to intermediate")
    assert analysis["topic"] == "backend development"
    assert analysis["current_level"] == "beginner"
    assert analysis["target_level"] == "intermediate"
    assert analysis["is_code_topic"] is True
    assert "no prior experience" in analysis["prior_knowledge"].lower()


def test_goal_analysis_indonesian_input() -> None:
    analysis = _analyze_goal("ajari saya python dari dasar ke menengah")
    assert "python" in analysis["topic"].lower()
    assert analysis["current_level"] == "beginner"
    assert analysis["target_level"] == "intermediate"


def test_backend_curriculum_has_finish_line_and_time_estimates() -> None:
    modules = _build_backend_curriculum("backend development")
    assert len(modules) >= 4
    step1 = modules[0]
    assert "HTTP" in step1["title"]
    assert step1["time_estimate"]
    assert "finish_line" in step1
    assert "GET /hello" in step1["finish_line"]
    assert '{"message": "hello"}' in step1["finish_line"]
    assert step1["exercise_code"]
    assert "FastAPI" in step1["exercise_code"]
    assert step1["explanation"]
    assert step1["summary"]


# --------------------------------------------------------------------------- #
# 2. Workflow Execution & Honesty Banner
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_learning_workflow_deterministic_run_with_stubs() -> None:
    wf = LearningWorkflow()
    task = TaskSpec(
        domain=Domain.LEARNING,
        goal="learn backend development from scratch to intermediate",
    )
    result = await wf.run(task)

    assert result.status == WorkflowStatus.COMPLETED
    assert result.error is None
    assert result.verification is not None
    assert result.verification.passed is True

    # Check artifacts
    artifact_types = {a.type for a in result.artifacts}
    assert "lesson.md" in artifact_types
    assert "curriculum.json" in artifact_types

    # Check honesty banner is present when collectors are stubs
    assert HONESTY_BANNER in result.output
    lesson_artifact = next(a for a in result.artifacts if a.type == "lesson.md")
    assert HONESTY_BANNER in lesson_artifact.content_ref


@pytest.mark.asyncio
async def test_learning_workflow_with_real_sources_omits_honesty_banner() -> None:
    async def real_collector(query: str):
        return [
            _item("web", "FastAPI Documentation", "https://fastapi.tiangolo.com/", 0.95, provider="http:brave"),
            _item("docs", "Python Official Tutorial", "https://docs.python.org/3/tutorial/", 0.92, provider="http:docs"),
        ]

    wf = LearningWorkflow(collectors=(real_collector,))
    task = TaskSpec(
        domain=Domain.LEARNING,
        goal="learn backend development from scratch to intermediate",
    )
    result = await wf.run(task)

    assert result.status == WorkflowStatus.COMPLETED
    assert result.verification is not None
    assert result.verification.passed is True
    # Real sources -> no simulation honesty banner
    assert HONESTY_BANNER not in result.output
    assert "https://fastapi.tiangolo.com/" in result.output


# --------------------------------------------------------------------------- #
# 3. Canvas Views & Astra UX Hierarchy
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_views_hierarchy_follows_astra_do_understand_reference() -> None:
    """Astra UX rule: Lead with runnable exercise, then lesson explanation, then sources."""
    wf = LearningWorkflow()
    task = TaskSpec(
        domain=Domain.LEARNING,
        goal="learn backend development from scratch to intermediate",
    )
    result = await wf.run(task)
    assert result.status == WorkflowStatus.COMPLETED

    state = wf.runner.state_store.load(task.task_id)
    assert state is not None
    node_views = state.data.get("node_views", [])
    # Astra 2nd ruling: 3 non-redundant views (no separate summary card)
    assert len(node_views) == 3

    # View 0: Exercise (code) - ACTIONABLE STEP FIRST
    v0 = node_views[0]
    assert v0["node_id"] == "exercise"
    assert v0["view"]["kind"] == "code"
    assert "Exercise:" in v0["view"]["title"]
    assert "GET /hello" in v0["view"]["caption"]
    assert "FastAPI" in v0["view"]["text"]

    # View 1: Lesson (markdown) - EXPLANATION & GUIDANCE
    v1 = node_views[1]
    assert v1["node_id"] == "lesson"
    assert v1["view"]["kind"] == "markdown"
    assert "Lesson:" in v1["view"]["title"]

    # View 2: Sources (markdown) - REFERENCE WITH REAL URLS
    v2 = node_views[2]
    assert v2["node_id"] == "sources"
    assert v2["view"]["kind"] == "markdown"
    assert "Sources" in v2["view"]["title"]
    assert HONESTY_BANNER in v2["view"]["text"]

    # Astra rule: No fabricated video view
    view_kinds = [item["view"]["kind"] for item in node_views]
    assert "video" not in view_kinds
    assert "Video view omitted because no verified, embeddable video lesson" in result.output


# --------------------------------------------------------------------------- #
# 4. Canonical Graph Specification
# --------------------------------------------------------------------------- #


def test_learning_graph_spec() -> None:
    wf = LearningWorkflow()
    spec = wf.graph_spec()

    assert spec["id"] == f"wf-{WORKFLOW_NAME}"
    assert spec["name"] == WORKFLOW_NAME
    assert len(spec["nodes"]) == 10

    node_ids = [n["id"] for n in spec["nodes"]]
    expected_order = [
        "input",
        "goal_analysis",
        "curriculum_planning",
        "source_gathering",
        "exercise",
        "lesson",
        "sources",
        "synthesis",
        "review",
        "output",
    ]
    assert node_ids == expected_order

    # Linear edges connecting all nodes
    assert len(spec["edges"]) == len(expected_order) - 1
    for i, edge in enumerate(spec["edges"]):
        assert edge["source"] == expected_order[i]
        assert edge["target"] == expected_order[i + 1]


# --------------------------------------------------------------------------- #
# 5. Template Catalog Registration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_learning_views_db_persistence_and_resolution(tmp_path) -> None:
    from sqlalchemy import text
    from uap.db import create_db_engine, create_session_factory
    from uap.db.engine import session_scope
    from uap.db.repositories import ExecutionRepository
    from uap.views.store import NodeViewStore

    url = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"
    try:
        engine = create_db_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"PostgreSQL uap_test not reachable: {exc}")

    session_factory = create_session_factory(engine)
    app = create_app(runs_dir=tmp_path, run_inline=True)
    # Wire session_factory into app state so execute_run uses DB persistence
    from uap.slice import PlatformSlice
    app.state.slice = PlatformSlice(session_factory=session_factory, artifacts_root=tmp_path)

    client = TestClient(app)
    response = client.post(
        "/tasks",
        json={"input": "learn backend development from scratch to intermediate"},
    )
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]

    # Resolve execution row from DB
    with session_scope(session_factory) as session:
        import uuid as _uuid
        repo = ExecutionRepository(session)
        row = repo.get_by_correlation_id(_uuid.UUID(task_id))
        assert row is not None, "Execution row should be created in DB"
        row_id = str(row.id)

        # Verify NodeViewStore persisted the views
        db_views = NodeViewStore(session).list_for_execution(row_id)
        assert len(db_views) == 3
        assert [v["view"]["kind"] for v in db_views] == ["code", "markdown", "markdown"]

    # Both correlation_id and durable row_id resolve the exact same views via API
    by_corr = client.get(f"/api/executions/{task_id}/views")
    by_row = client.get(f"/api/executions/{row_id}/views")
    assert by_corr.status_code == 200
    assert by_row.status_code == 200
    assert by_corr.json() == by_row.json()
    assert len(by_corr.json()) == 3


def test_learning_template_registered_in_catalog() -> None:
    tpl = get_template("interactive_backend_learning")
    assert tpl is not None
    assert tpl["workflow"]["domain"] == "learning"
    assert tpl["workflow"]["workflow_ref"] == "learning"
    assert "backend" in tpl["name"]
    assert "learn" in tpl["input"]["example"]
    assert tpl["input"]["required"] == ["input"]


# --------------------------------------------------------------------------- #
# 6. Router & Server API Integration
# --------------------------------------------------------------------------- #


def test_router_executable_domains_includes_learning() -> None:
    assert Domain.LEARNING in EXECUTABLE_DOMAINS


def test_api_task_execution_and_views(tmp_path) -> None:
    app = create_app(runs_dir=tmp_path, run_inline=True)
    client = TestClient(app)

    # 1. Submit learning task
    response = client.post(
        "/tasks",
        json={"input": "learn backend development from scratch to intermediate"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "accepted"
    assert data["domain"] == "learning"
    task_id = data["task_id"]

    # 2. Check task detail
    task_res = client.get(f"/tasks/{task_id}")
    assert task_res.status_code == 200
    task_detail = task_res.json()
    assert task_detail["status"] == "completed"
    assert len(task_detail["artifacts"]) >= 2

    # 3. Check GET /api/executions/{id}/views
    views_res = client.get(f"/api/executions/{task_id}/views")
    assert views_res.status_code == 200, views_res.text
    views = views_res.json()
    assert len(views) == 3

    kinds = [v["view"]["kind"] for v in views]
    assert kinds == ["code", "markdown", "markdown"]

    titles = [v["view"]["title"] for v in views]
    assert any("Exercise:" in t for t in titles)
    assert any("Lesson:" in t for t in titles)
    assert any("Sources" in t for t in titles)
    assert not any("Summary:" in t for t in titles)

    # 4. Check GET /api/executions/{id}/graph
    graph_res = client.get(f"/api/executions/{task_id}/graph")
    assert graph_res.status_code == 200
    graph = graph_res.json()
    assert graph["id"] == "wf-learning"
    assert len(graph["nodes"]) == 10
