"""Tests for ContextCompiler wiring on the real execution path.

Covers:
1. Compiled context reaches the agent (LLM client receives compiled sections).
2. Budget enforced (huge prior output truncated/evicted, size within budget).
3. Determinism (same state -> byte-identical compiled sections).
4. Knowledge integration (provenance refs with fake store; fallback without).
5. Inspectable surface filters secrets (no credentials pass through).
6. Slice integration & endpoint inspectability.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uap.agents.llm import LLMAgent
from uap.agents.registry import AgentRegistry
from uap.context import ContextBudget, ContextCompiler
from uap.contracts import (
    AgentContext,
    AgentStatus,
    ContextSection,
    TaskSpec,
    TokenUsage,
    WorkflowState,
    utc_now,
)
from uap.graph import GraphNode, NodeKind, Port, PortType
from uap.knowledge.model import KnowledgeItem, KnowledgeStatus, Provenance
from uap.server.app import create_app
from uap.slice import PlatformNodeRuntime, PlatformSlice, build_research_graph
from uap.slice.orchestrator import _llm_synthesizer
from uap.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# Spies and Fixtures
# --------------------------------------------------------------------------- #


class SpyChatClient:
    """Records complete() calls to verify prompt contents."""

    def __init__(self, response_text: str = "Analysis from spy LLM.") -> None:
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, model: str, messages: list[dict[str, str]], **kwargs: Any
    ) -> tuple[str, TokenUsage]:
        self.calls.append({"model": model, "messages": list(messages), "kwargs": kwargs})
        return self.response_text, TokenUsage(tokens_in=50, tokens_out=25)


def _port(name: str = "value") -> Port:
    return Port(name=name, type=PortType.ANY, required=True)


def _make_runtime(
    *,
    agents: AgentRegistry | None = None,
    tools: ToolRegistry | None = None,
    budget: ContextBudget | None = None,
    knowledge_store: Any | None = None,
    session_factory: Any | None = None,
    pipeline: Any | None = None,
    task: TaskSpec | None = None,
) -> PlatformNodeRuntime:
    agents = agents or AgentRegistry()
    tools = tools or ToolRegistry()
    return PlatformNodeRuntime(
        agents,
        tools,
        task=task or TaskSpec(domain="research", goal="Research caching strategies"),
        pipeline=pipeline,
        session_factory=session_factory,
        knowledge_store=knowledge_store,
        budget=budget,
    )


# --------------------------------------------------------------------------- #
# DB Setup for Slice Tests
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


# --------------------------------------------------------------------------- #
# 1. Compiled Context Reaches the Agent (LLM messages contain compiled sections)
# --------------------------------------------------------------------------- #


def test_compiled_context_reaches_agent_llm() -> None:
    """Spy on LLMAgent: messages must contain compiled sections, not just raw question."""
    spy = SpyChatClient()
    agents = AgentRegistry()
    agent = LLMAgent(client=spy)
    agents.register(agent)

    runtime = _make_runtime(agents=agents)
    runtime._pipeline_data["question"] = "What are caching trade-offs?"
    runtime._pipeline_data["prior_evidence"] = "Redis has sub-millisecond latency."

    node = GraphNode(
        id="analyst",
        kind=NodeKind.AGENT,
        inputs=[_port()],
        outputs=[_port()],
        config={"agent": "llm"},
    )
    from uap.execution.context import ExecutionContext

    ctx = ExecutionContext(execution_id="test-exec", graph=build_research_graph())
    output = asyncio.run(runtime.run_node(node, {"value": "What are caching trade-offs?"}, ctx))

    assert "value" in output
    assert output["value"]["agent"] == "llm"
    assert len(spy.calls) == 1

    messages = spy.calls[0]["messages"]
    user_msg = next(m["content"] for m in messages if m["role"] == "user")

    # Assert compiled sections reach the LLM prompt
    assert "## task_state" in user_msg or "## prior:prior_evidence" in user_msg
    assert "Redis has sub-millisecond latency" in user_msg


def test_compiled_context_reaches_synthesizer_llm() -> None:
    """Spy on synthesizer callable: prompt messages include compiled context sections."""
    spy = SpyChatClient()
    synthesizer = _llm_synthesizer(spy)

    from uap.workflows.research import ResearchWorkflow

    pipeline = ResearchWorkflow(synthesizer=synthesizer)
    runtime = _make_runtime(pipeline=pipeline)
    runtime._pipeline_data["question"] = "Explain event-driven architecture"
    runtime._pipeline_data["verified_claims"] = [
        {"claim": "Kafka provides partitioned logs", "sources": ["web"], "evidence": []}
    ]
    node = GraphNode(
        id="synthesis",
        kind=NodeKind.SYNTHESIS,
        inputs=[_port()],
        outputs=[_port()],
        config={"pipeline_node": "synthesis"},
    )

    output = asyncio.run(
        runtime._run_pipeline_node(
            "synthesis",
            node,
            {"value": "Explain event-driven architecture"},
        )
    )

    assert "value" in output
    assert len(spy.calls) == 1

    messages = spy.calls[0]["messages"]
    user_msg = next(m["content"] for m in messages if m["role"] == "user")

    # The user message must contain compiled context sections, not just raw question
    assert "## Context" in user_msg
    assert "task_state" in user_msg or "prior:" in user_msg
    assert "Kafka provides partitioned logs" in user_msg


# --------------------------------------------------------------------------- #
# 2. Budget Enforced (Truncation & Eviction)
# --------------------------------------------------------------------------- #


def test_budget_enforced_truncation_and_eviction() -> None:
    """Huge prior-node output gets truncated/evicted; size stays within budget."""
    budget = ContextBudget(max_sections=3, max_chars_per_section=150, max_total_chars=400)
    runtime = _make_runtime(budget=budget)

    # 4 huge outputs (would total > 40,000 chars without budget)
    runtime._pipeline_data["huge_node_1"] = "X" * 10_000
    runtime._pipeline_data["huge_node_2"] = "Y" * 10_000
    runtime._pipeline_data["huge_node_3"] = "Z" * 10_000
    runtime._pipeline_data["huge_node_4"] = "W" * 10_000

    compiled = runtime.compile_context()

    # 1. Total sections cap enforced
    assert len(compiled.context_sections) <= budget.max_sections
    assert len(compiled.context_sections) <= 3

    # 2. Per-section character cap enforced (truncation)
    for section in compiled.context_sections:
        assert len(section.content) <= budget.max_chars_per_section

    # 3. Total character cap enforced (eviction)
    total_chars = sum(len(s.content) for s in compiled.context_sections)
    assert total_chars <= budget.max_total_chars

    # 4. Eviction occurred (dropped count > 0)
    assert compiled.extras["dropped_count"] > 0


# --------------------------------------------------------------------------- #
# 3. Determinism (Byte-identical ordering)
# --------------------------------------------------------------------------- #


def test_context_compilation_determinism() -> None:
    """Same state -> same compiled sections (byte-identical ordering and content)."""
    state_data = {
        "question": "Compare Postgres and SQLite",
        "plan": {"collectors": ["web", "docs"], "strategy": "parallel"},
        "evidence": [
            {"source": "docs", "claim": "Postgres supports concurrent writes", "confidence": 0.9},
            {"source": "web", "claim": "SQLite is serverless and lightweight", "confidence": 0.85},
        ],
        "filtered_evidence": [
            {"claim": "Postgres supports concurrent writes", "confidence": 0.9},
            {"claim": "SQLite is serverless and lightweight", "confidence": 0.85},
        ],
    }

    runtime1 = _make_runtime()
    runtime1._pipeline_data = dict(state_data)
    ctx1 = runtime1.compile_context()

    runtime2 = _make_runtime()
    runtime2._pipeline_data = dict(state_data)
    ctx2 = runtime2.compile_context()

    dump1 = [s.model_dump() for s in ctx1.context_sections]
    dump2 = [s.model_dump() for s in ctx2.context_sections]

    assert dump1 == dump2
    assert json.dumps(dump1, sort_keys=True) == json.dumps(dump2, sort_keys=True)
    assert [s.key for s in ctx1.context_sections] == [s.key for s in ctx2.context_sections]


# --------------------------------------------------------------------------- #
# 4. Knowledge Integration (Provenance refs & fallback)
# --------------------------------------------------------------------------- #


class FakeKnowledgeStore:
    def __init__(self, items: list[KnowledgeItem]) -> None:
        self._items = items

    def search(self, query: str) -> list[tuple[KnowledgeItem, float]]:
        return [(item, 0.95) for item in self._items]


def test_knowledge_integration_with_provenance_refs() -> None:
    """With fake knowledge store returning items, provenance refs appear in sections."""
    item = KnowledgeItem(
        knowledge_id="k-100",
        statement="Write-ahead logging ensures durability",
        domain="research",
        status=KnowledgeStatus.PROMOTED,
        provenance=[
            Provenance(
                source_kind="artifact",
                source_ref="art-wal-spec",
                extracted_by="researcher",
                extracted_at=utc_now(),
                evidence="WAL writes changes before committing",
            )
        ],
        confidence=0.95,
        tags=["wal", "durability"],
    )

    fake_store = FakeKnowledgeStore([item])
    runtime = _make_runtime(
        knowledge_store=fake_store,
        task=TaskSpec(domain="research", goal="How does WAL guarantee durability?"),
    )
    runtime._pipeline_data["question"] = "How does WAL guarantee durability?"

    compiled = runtime.compile_context()

    # Knowledge item appears as a section
    knowledge_sections = [
        s for s in compiled.context_sections if s.key.startswith("knowledge:")
    ]
    assert len(knowledge_sections) >= 1
    k_sec = knowledge_sections[0]

    # Provenance ref appears in the section content
    assert "artifact:art-wal-spec" in k_sec.content
    assert "Write-ahead logging ensures durability" in k_sec.content

    # Inspectable surface includes source_ref
    inspectable = runtime.get_inspectable_context()
    k_inspectable = next(
        s for s in inspectable["sections"] if s["key"].startswith("knowledge:")
    )
    assert "artifact:art-wal-spec" in k_inspectable["source_ref"]


def test_knowledge_integration_deterministic_fallback() -> None:
    """Without knowledge store or PG session, compilation still succeeds deterministically."""
    runtime = _make_runtime(knowledge_store=None, session_factory=None)
    runtime._pipeline_data["question"] = "Query with no store available"
    runtime._pipeline_data["cached_result"] = "Some prior cached value"

    compiled = runtime.compile_context()

    assert compiled is not None
    assert isinstance(compiled, AgentContext)
    # Sections compiled from state only
    assert any("task_state" in s.key for s in compiled.context_sections)
    # No knowledge sections
    assert not any(s.key.startswith("knowledge:") for s in compiled.context_sections)


# --------------------------------------------------------------------------- #
# 5. Inspectable Surface Filters Secrets
# --------------------------------------------------------------------------- #


def test_inspectable_surface_filters_secrets() -> None:
    """Inspectable surface returns keys + sizes + source refs WITHOUT secret content."""
    runtime = _make_runtime()

    secret_key_val = "sk-live-super-secret-key-1234567890"
    db_pass_val = "super-secret-postgres-password-999"
    api_token_val = "bearer-token-abc-xyz-token"

    runtime._pipeline_data["api_key"] = secret_key_val
    runtime._pipeline_data["password"] = db_pass_val
    runtime._pipeline_data["auth_token"] = api_token_val
    runtime._pipeline_data["public_summary"] = "Safe public research findings"

    runtime.compile_context()
    inspectable = runtime.get_inspectable_context()

    # 1. Structure check: keys + sizes + source refs
    assert "sections" in inspectable
    assert "total_chars" in inspectable
    assert "budget" in inspectable

    for sec in inspectable["sections"]:
        assert "key" in sec
        assert "size" in sec
        assert "source_ref" in sec

    # 2. Assert no sensitive keys survived
    sec_keys = [s["key"].lower() for s in inspectable["sections"]]
    assert not any("api_key" in k for k in sec_keys)
    assert not any("password" in k for k in sec_keys)
    assert not any("auth_token" in k for k in sec_keys)

    # 3. Assert no raw credential strings pass through
    serialized = json.dumps(inspectable)
    assert secret_key_val not in serialized
    assert db_pass_val not in serialized
    assert api_token_val not in serialized

    # 4. Safe data is present
    assert any("public_summary" in s["key"] for s in inspectable["sections"])


# --------------------------------------------------------------------------- #
# 6. Slice Integration & Context Endpoint Inspectability
# --------------------------------------------------------------------------- #


@db_test
def test_slice_execution_context_inspectable_via_endpoint(db_session_factory, tmp_path) -> None:
    """Full slice execution produces inspectable context readable via GET /api/executions/{id}/context."""
    app = create_app(runs_dir=tmp_path, run_inline=True)
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
    )
    app.state.slice = slice_

    result = slice_.run("research and compare two approaches to caching")
    assert result.error is None, result.error
    assert result.execution_status == "completed"

    # Context is attached to SliceResult
    assert result.context is not None
    assert "sections" in result.context
    assert len(result.context["sections"]) > 0

    # Test the API endpoint
    client = TestClient(app)

    resp = client.get(f"/api/executions/{result.execution_id}/context")
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["execution_id"] == result.execution_id
    assert "sections" in data
    assert len(data["sections"]) > 0
    assert "total_chars" in data
    assert data["total_chars"] > 0
    assert "budget" in data

    for sec in data["sections"]:
        assert "key" in sec
        assert "size" in sec
        assert "source_ref" in sec

    # Test unknown execution returns 404
    resp_404 = client.get("/api/executions/00000000-0000-0000-0000-000000000000/context")
    assert resp_404.status_code == 404


def test_task_submission_context_endpoint(tmp_path) -> None:
    """Task submitted via POST /tasks has inspectable context via GET /api/executions/{task_id}/context."""
    app = create_app(runs_dir=tmp_path, run_inline=True)
    client = TestClient(app)

    resp = client.post("/tasks", json={"input": "research database indexing strategies", "user_id": "tester"})
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]

    ctx_resp = client.get(f"/api/executions/{task_id}/context")
    assert ctx_resp.status_code == 200, ctx_resp.text

    data = ctx_resp.json()
    assert data["execution_id"] == task_id
    assert "sections" in data
    assert len(data["sections"]) > 0
    assert data["total_chars"] > 0
    for sec in data["sections"]:
        assert "key" in sec
        assert "size" in sec
        assert "source_ref" in sec
