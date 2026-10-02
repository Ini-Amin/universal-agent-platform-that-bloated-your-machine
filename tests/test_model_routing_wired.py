"""Tests for ModelRouter wiring onto the real execution path.

Verifies:
1. LLMAgent uses the router's chosen model.
2. The routing decision is observable with a reason string (trace and decision objects).
3. Capability mismatch results in honest, visible fallback.
4. Without a router, UAP_DEFAULT_MODEL is used (regression guarantee).
5. UAP_MODEL_CATALOG override is honoured by the router.
6. The server wires a ModelRouter by default when a catalog is available.
7. Slice records MODEL_SELECTION DecisionTrace in durable DB store.
8. Synthesizer uses router when provided and degrades honestly.
9. select_or_fallback logs a warning and returns an explicit fallback decision.
10. DecisionTrace alternatives capture ranked fallback candidates.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from uap.agents.llm import LLMAgent
from uap.contracts import AgentContext, TaskSpec
from uap.contracts.models import TokenUsage
from uap.models.catalog import ModelCapability, ModelCatalog, ModelInfo
from uap.models.client import ChatClient
from uap.models.policy import BudgetPolicy, PolicyResolver
from uap.models.router import ModelRouter, RoutingDecision, RoutingRequest
from uap.server.app import _build_llm_synthesizer, create_app
from uap.slice import PlatformSlice
from uap.trace.model import DecisionTrace, DecisionType
from uap.trace.store import TraceStore


# --------------------------------------------------------------------------- #
# Helpers & Spies
# --------------------------------------------------------------------------- #


class SpyChatClient(ChatClient):
    """Spy chat client that records model and messages without network calls."""

    def __init__(self, response_text: str = "Spy response content"):
        super().__init__(base_url="http://127.0.0.1:9999", api_key="test-key")
        self.calls: list[dict[str, Any]] = []
        self.response_text = response_text

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[str, TokenUsage]:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return self.response_text, TokenUsage(tokens_in=10, tokens_out=20, latency_ms=5.0)


def _make_test_catalog() -> ModelCatalog:
    """Deterministic catalog with distinguishable models."""
    return ModelCatalog(
        [
            ModelInfo(
                id="cheap-reasoner",
                provider="local",
                capabilities=[ModelCapability.REASONING, ModelCapability.FAST],
                context_window=128_000,
                cost_per_1k_in=0.001,
                cost_per_1k_out=0.002,
                latency_class="fast",
            ),
            ModelInfo(
                id="expensive-reasoner",
                provider="local",
                capabilities=[ModelCapability.REASONING],
                context_window=200_000,
                cost_per_1k_in=0.010,
                cost_per_1k_out=0.020,
                latency_class="slow",
            ),
            ModelInfo(
                id="coder-only",
                provider="local",
                capabilities=[ModelCapability.CODING],
                context_window=64_000,
                cost_per_1k_in=0.005,
                cost_per_1k_out=0.010,
                latency_class="standard",
            ),
        ]
    )


# --------------------------------------------------------------------------- #
# DB Fixture (same as test_slice_llm.py / test_vertical_slice.py)
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
    except Exception as exc:
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
# Unit & Integration Tests (>= 8)
# --------------------------------------------------------------------------- #


def test_1_llmagent_calls_router_chosen_model():
    """1. With a router injected, LLMAgent calls the model the router chose."""
    catalog = _make_test_catalog()
    router = ModelRouter(catalog, PolicyResolver())
    spy_client = SpyChatClient()

    agent = LLMAgent(client=spy_client, router=router)
    context = AgentContext(task=TaskSpec(domain="research", goal="analyze algorithms"))

    result = asyncio.run(agent.run(context))
    assert result.status.value == "success"
    assert len(spy_client.calls) == 1
    # Cost preference picks "cheap-reasoner" over "expensive-reasoner"
    assert spy_client.calls[0]["model"] == "cheap-reasoner"
    assert agent.last_decision is not None
    assert agent.last_decision.model_id == "cheap-reasoner"


def test_2_routing_decision_recorded_with_reason_string():
    """2. The routing decision is recorded (trace and decision object) with a reason string."""
    catalog = _make_test_catalog()
    router = ModelRouter(catalog, PolicyResolver())
    spy_client = SpyChatClient()
    traces_received: list[DecisionTrace] = []

    agent = LLMAgent(
        client=spy_client,
        router=router,
        decision_sink=traces_received.append,
    )
    context = AgentContext(task=TaskSpec(domain="research", goal="solve problem"))

    asyncio.run(agent.run(context))

    # Decision object verification
    assert agent.last_decision is not None
    assert isinstance(agent.last_decision.reason, str)
    assert len(agent.last_decision.reason) > 0
    assert "cheap-reasoner" in agent.last_decision.reason
    assert "reasoning" in agent.last_decision.reason

    # DecisionTrace verification
    assert agent.last_trace is not None
    assert agent.last_trace.decision_type is DecisionType.MODEL_SELECTION
    assert agent.last_trace.chosen == "cheap-reasoner"
    assert agent.last_trace.rationale == agent.last_decision.reason
    assert agent.last_trace.confidence == 1.0

    # Decision sink verification
    assert len(traces_received) == 1
    assert traces_received[0] == agent.last_trace


def test_3_capability_mismatch_visible_fallback(caplog):
    """3. Capability mismatch: requesting a capability absent from catalog -> visible fallback."""
    catalog = _make_test_catalog()  # Only REASONING, FAST, CODING; NO VISION
    router = ModelRouter(catalog, PolicyResolver())
    spy_client = SpyChatClient()

    agent = LLMAgent(client=spy_client, router=router)
    context = AgentContext(
        task=TaskSpec(domain="research", goal="classify image"),
        extras={"capability": "vision"},
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(agent.run(context))

    assert result.status.value == "success"
    # Fallback must be honest and visible, not a silent switch
    assert agent.fallback_reason is not None
    assert "explicit fallback to" in agent.fallback_reason
    assert "capability=vision" in agent.fallback_reason
    assert agent.last_fallback is not None
    assert agent.last_fallback["capability"] == "vision"
    assert "explicit fallback to" in agent.last_decision.reason

    # Warning was logged
    assert any("cannot satisfy capability 'vision'" in r.message for r in caplog.records)

    # Explicit fallback model was called
    expected_default = os.environ.get("UAP_DEFAULT_MODEL", "gpt-5.6-sol")
    assert spy_client.calls[0]["model"] == expected_default
    assert agent.last_trace.inputs_summary["fallback"] is True


def test_4_without_router_uses_uap_default_model(monkeypatch):
    """4. Without a router -> UAP_DEFAULT_MODEL used (regression guard)."""
    monkeypatch.setenv("UAP_DEFAULT_MODEL", "custom-env-model-default")
    spy_client = SpyChatClient()

    agent = LLMAgent(client=spy_client, router=None)
    context = AgentContext(task=TaskSpec(domain="research", goal="test regression"))

    result = asyncio.run(agent.run(context))
    assert result.status.value == "success"
    assert len(spy_client.calls) == 1
    assert spy_client.calls[0]["model"] == "custom-env-model-default"
    assert agent.last_decision is None
    assert agent.fallback_reason is None
    assert agent.last_trace is None


def test_5_uap_model_catalog_override_honoured(monkeypatch):
    """5. UAP_MODEL_CATALOG override is honoured by the router."""
    monkeypatch.setenv(
        "UAP_MODEL_CATALOG",
        "custom-provider/special-model-1,custom-provider/special-model-2",
    )
    catalog = ModelCatalog.default()
    router = ModelRouter(catalog, PolicyResolver())
    spy_client = SpyChatClient()

    agent = LLMAgent(client=spy_client, router=router)
    context = AgentContext(task=TaskSpec(domain="research", goal="test override"))

    asyncio.run(agent.run(context))
    assert len(spy_client.calls) == 1
    chosen = spy_client.calls[0]["model"]
    assert chosen in ["custom-provider/special-model-1", "custom-provider/special-model-2"]
    assert agent.last_decision.model_id == chosen


def test_6_server_wires_router_by_default(tmp_path):
    """6. The server wires a router by default when a catalog is available."""
    app = create_app(runs_dir=tmp_path / "runs", run_inline=True)
    assert hasattr(app.state, "model_router")
    assert app.state.model_router is not None
    assert isinstance(app.state.model_router, ModelRouter)
    assert app.state.model_router.catalog is not None
    assert len(app.state.model_router.catalog.all()) > 0

    # When slice is available, it receives the same router instance
    if getattr(app.state, "slice", None) is not None:
        assert app.state.slice._model_router is app.state.model_router


@db_test
def test_7_slice_records_model_selection_decision_trace_in_db(db_session_factory, tmp_path):
    """7. Slice records MODEL_SELECTION DecisionTrace in durable DB store."""
    catalog = _make_test_catalog()
    router = ModelRouter(catalog, PolicyResolver())
    spy_client = SpyChatClient()

    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path / "artifacts",
        llm_client=spy_client,
        model_router=router,
    )

    result = slice_.run("research strategies for distributed caching")
    assert result.error is None
    assert result.execution_status == "completed"
    assert result.traces_recorded >= 2  # ROUTING + MODEL_SELECTION

    # Query durable decision_traces table
    with db_session_factory() as session:
        traces = TraceStore(session).list_for_execution(result.execution_id)

    model_traces = [t for t in traces if t.decision_type is DecisionType.MODEL_SELECTION]
    assert len(model_traces) >= 1
    trace = model_traces[0]
    assert trace.chosen == "cheap-reasoner"
    assert "cheap-reasoner" in trace.rationale
    assert trace.node_id == "synthesis"


def test_8_legacy_synthesizer_uses_router_model(monkeypatch):
    """8. Legacy synthesizer uses router when provided and falls back to default when None."""
    monkeypatch.setenv("UAP_LLM_API_KEY", "mock-key")
    monkeypatch.setenv("UAP_DEFAULT_MODEL", "env-default-model")
    catalog = _make_test_catalog()
    router = ModelRouter(catalog, PolicyResolver())

    captured_models: list[str] = []

    class MockChatClient:
        async def complete(self, model, messages, **kwargs):
            captured_models.append(model)
            return "Analysis text", TokenUsage(tokens_in=5, tokens_out=5)

        async def aclose(self):
            pass

    with patch("uap.models.client.ChatClient", MockChatClient):
        # With router:
        synth_with_router = _build_llm_synthesizer(router=router)
        assert synth_with_router is not None
        asyncio.run(synth_with_router("what is caching?", [{"claim": "caching is fast"}]))
        assert captured_models[-1] == "cheap-reasoner"

        # Without router:
        synth_without_router = _build_llm_synthesizer(router=None)
        assert synth_without_router is not None
        asyncio.run(synth_without_router("what is caching?", [{"claim": "caching is fast"}]))
        assert captured_models[-1] == "env-default-model"


def test_9_router_select_or_fallback_direct_logging(caplog):
    """9. select_or_fallback logs a warning and returns an explicit fallback decision."""
    catalog = _make_test_catalog()
    router = ModelRouter(catalog, PolicyResolver())

    with caplog.at_level(logging.WARNING):
        decision, is_fallback = router.select_or_fallback(
            RoutingRequest(capability=ModelCapability.EMBEDDING),
            fallback_model="custom-fallback-3",
        )

    assert is_fallback is True
    assert decision.model_id == "custom-fallback-3"
    assert "explicit fallback to custom-fallback-3" in decision.reason
    assert any("cannot satisfy capability 'embedding'" in r.message for r in caplog.records)


def test_10_decision_trace_contains_fallbacks_as_alternatives():
    """10. DecisionTrace alternatives capture ranked fallback candidates."""
    catalog = _make_test_catalog()
    router = ModelRouter(catalog, PolicyResolver())
    spy_client = SpyChatClient()

    agent = LLMAgent(client=spy_client, router=router)
    context = AgentContext(task=TaskSpec(domain="research", goal="rank options"))

    asyncio.run(agent.run(context))
    assert agent.last_trace is not None
    # "expensive-reasoner" should be listed in alternatives
    alt_options = [alt.option for alt in agent.last_trace.alternatives]
    assert "expensive-reasoner" in alt_options
    for alt in agent.last_trace.alternatives:
        assert isinstance(alt.reason_rejected, str)
        assert len(alt.reason_rejected) > 0
