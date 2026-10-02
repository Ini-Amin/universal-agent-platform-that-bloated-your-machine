"""Tests for domain-based model capability routing (Master section 33).

Verifies:
1. Each domain maps to the documented capability (data-driven mapping table).
2. A research task routes to a REASONING-capable model.
3. A task flagged as fast routes to a FAST-capable model.
4. A capability absent from the catalog produces the explicit fallback signal (not a silent switch).
5. The decision trace records the capability and the reason.
6. UAP_MODEL_CATALOG override is honoured end to end.
7. A coding task routes to a CODING-capable model.
8. Fallback decision traces record confidence=0.0 and fallback=True.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from uap.contracts import AgentContext, Domain, TaskSpec
from uap.contracts.models import TokenUsage
from uap.models.catalog import ModelCapability, ModelCatalog, ModelInfo
from uap.models.client import ChatClient
from uap.models.policy import BudgetPolicy, PolicyResolver
from uap.models.router import (
    DOMAIN_CAPABILITY_MAP,
    DOMAIN_RATIONALE,
    ModelRouter,
    RoutingDecision,
    RoutingRequest,
    capability_for,
)
from uap.slice.orchestrator import PlatformSlice, _llm_synthesizer
from uap.trace.model import DecisionTrace, DecisionType
from uap.trace.store import TraceStore


# --------------------------------------------------------------------------- #
# Helpers & Spies
# --------------------------------------------------------------------------- #


class SpyChatClient(ChatClient):
    """Spy chat client recording model and messages without network calls."""

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


def _make_multi_capability_catalog() -> ModelCatalog:
    """Deterministic catalog with models dedicated to specific capabilities."""
    return ModelCatalog(
        [
            ModelInfo(
                id="deep-reasoner",
                provider="local",
                capabilities=[ModelCapability.REASONING],
                context_window=200_000,
                cost_per_1k_in=0.010,
                cost_per_1k_out=0.020,
                latency_class="slow",
            ),
            ModelInfo(
                id="flash-summarizer",
                provider="local",
                capabilities=[ModelCapability.FAST],
                context_window=128_000,
                cost_per_1k_in=0.001,
                cost_per_1k_out=0.002,
                latency_class="fast",
            ),
            ModelInfo(
                id="code-specialist",
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
# DB Fixture (PostgreSQL connection for durable trace verification)
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
# Tests
# --------------------------------------------------------------------------- #


def test_1_each_domain_maps_to_documented_capability():
    """1. Each domain maps to the documented capability via data-driven table."""
    # Ensure the domain mapping table is data-driven and explicitly structured
    assert isinstance(DOMAIN_CAPABILITY_MAP, dict)
    assert DOMAIN_CAPABILITY_MAP["research"] == ModelCapability.REASONING
    assert DOMAIN_CAPABILITY_MAP["bbp"] == ModelCapability.REASONING
    assert DOMAIN_CAPABILITY_MAP["coding"] == ModelCapability.CODING
    assert DOMAIN_CAPABILITY_MAP["learning"] == ModelCapability.REASONING
    assert DOMAIN_CAPABILITY_MAP["data"] == ModelCapability.FAST
    assert DOMAIN_CAPABILITY_MAP["unknown"] == ModelCapability.REASONING

    # Ensure Domain enum exists and maps consistently
    assert Domain.RESEARCH.value in DOMAIN_CAPABILITY_MAP
    assert Domain.BBP.value in DOMAIN_CAPABILITY_MAP
    assert Domain.CODING.value in DOMAIN_CAPABILITY_MAP
    assert Domain.LEARNING.value in DOMAIN_CAPABILITY_MAP
    assert Domain.DATA.value in DOMAIN_CAPABILITY_MAP

    # Rationale must be documented for each domain
    for domain_name in DOMAIN_CAPABILITY_MAP:
        assert domain_name in DOMAIN_RATIONALE
        assert len(DOMAIN_RATIONALE[domain_name]) > 0

    # capability_for helper works with both string and enum
    assert capability_for("research") == ModelCapability.REASONING
    assert capability_for(Domain.RESEARCH) == ModelCapability.REASONING
    assert capability_for("bbp") == ModelCapability.REASONING
    assert capability_for(Domain.BBP) == ModelCapability.REASONING
    assert capability_for("coding") == ModelCapability.CODING
    assert capability_for(Domain.CODING) == ModelCapability.CODING
    assert capability_for("learning") == ModelCapability.REASONING
    assert capability_for(Domain.LEARNING) == ModelCapability.REASONING
    assert capability_for("data") == ModelCapability.FAST
    assert capability_for(Domain.DATA) == ModelCapability.FAST

    # Unmapped/unknown domain safely defaults to REASONING
    assert capability_for("unrecognized_domain") == ModelCapability.REASONING
    assert capability_for(None) == ModelCapability.REASONING


def test_2_research_task_routes_to_reasoning_capable_model():
    """2. A research task routes to a REASONING-capable model."""
    catalog = _make_multi_capability_catalog()
    router = ModelRouter(catalog, PolicyResolver())

    task = TaskSpec(
        domain=Domain.RESEARCH,
        goal="investigate consensus protocols in distributed systems",
    )
    cap = capability_for(task.domain, task.goal)
    assert cap == ModelCapability.REASONING

    decision, is_fallback = router.select_or_fallback(RoutingRequest(capability=cap))
    assert not is_fallback
    assert decision.model_id == "deep-reasoner"
    assert "reasoning" in decision.reason

    # BBP (security recon) also routes to REASONING
    bbp_task = TaskSpec(domain=Domain.BBP, goal="assess target attack surface")
    bbp_cap = capability_for(bbp_task.domain, bbp_task.goal)
    assert bbp_cap == ModelCapability.REASONING
    bbp_decision, bbp_is_fallback = router.select_or_fallback(RoutingRequest(capability=bbp_cap))
    assert not bbp_is_fallback
    assert bbp_decision.model_id == "deep-reasoner"

    # Also test via synthesizer with router
    spy_client = SpyChatClient()
    synth = _llm_synthesizer(spy_client, router=router)
    asyncio.run(
        synth(
            "explain Raft consensus",
            [{"claim": "Raft uses leader election"}],
            context=AgentContext(task=task),
        )
    )
    assert len(spy_client.calls) == 1
    assert spy_client.calls[0]["model"] == "deep-reasoner"


def test_3_task_flagged_as_fast_routes_to_fast_capable_model():
    """3. A task flagged as fast routes to a FAST-capable model."""
    catalog = _make_multi_capability_catalog()
    router = ModelRouter(catalog, PolicyResolver())

    # Case A: Explicit fast constraint flag
    task_flagged = TaskSpec(
        domain=Domain.RESEARCH,
        goal="research caching strategies",
        constraints={"fast": True},
    )
    cap_flagged = capability_for(
        task_flagged.domain,
        task_flagged.goal,
        constraints=task_flagged.constraints,
    )
    assert cap_flagged == ModelCapability.FAST
    decision, is_fb = router.select_or_fallback(RoutingRequest(capability=cap_flagged))
    assert not is_fb
    assert decision.model_id == "flash-summarizer"

    # Case B: Summarisation keyword in goal
    task_summary = TaskSpec(
        domain=Domain.RESEARCH,
        goal="summarize literature on database indexes",
    )
    cap_summary = capability_for(task_summary.domain, task_summary.goal)
    assert cap_summary == ModelCapability.FAST
    decision_summary, _ = router.select_or_fallback(RoutingRequest(capability=cap_summary))
    assert decision_summary.model_id == "flash-summarizer"

    # Case C: Speed keyword in goal
    task_quick = TaskSpec(
        domain=Domain.RESEARCH,
        goal="quick overview of findings",
    )
    cap_quick = capability_for(task_quick.domain, task_quick.goal)
    assert cap_quick == ModelCapability.FAST
    decision_quick, _ = router.select_or_fallback(RoutingRequest(capability=cap_quick))
    assert decision_quick.model_id == "flash-summarizer"

    # Case D: Prefer latency constraint
    task_latency = TaskSpec(
        domain=Domain.RESEARCH,
        goal="extract facts",
        constraints={"prefer": "latency"},
    )
    cap_latency = capability_for(task_latency.domain, task_latency.goal, constraints=task_latency.constraints)
    assert cap_latency == ModelCapability.FAST

    # Synthesizer integration test with fast goal
    spy_client = SpyChatClient()
    synth = _llm_synthesizer(spy_client, router=router)
    asyncio.run(
        synth(
            "summarize cache performance",
            [{"claim": "LRU reduces latency"}],
            context=AgentContext(task=task_summary),
        )
    )
    assert len(spy_client.calls) == 1
    assert spy_client.calls[0]["model"] == "flash-summarizer"


def test_4_capability_absent_produces_explicit_fallback_signal(caplog):
    """4. A capability absent from the catalog produces the explicit fallback signal (not silent switch)."""
    # Catalog with ONLY REASONING model; FAST and CODING capabilities are absent
    catalog_reasoner_only = ModelCatalog(
        [
            ModelInfo(
                id="only-reasoner",
                provider="local",
                capabilities=[ModelCapability.REASONING],
                context_window=128_000,
            )
        ]
    )
    router = ModelRouter(catalog_reasoner_only, PolicyResolver())

    # Task requiring CODING
    coding_cap = capability_for(Domain.CODING, "implement a binary search tree")
    assert coding_cap == ModelCapability.CODING

    with caplog.at_level(logging.WARNING):
        decision, is_fallback = router.select_or_fallback(
            RoutingRequest(capability=coding_cap),
            fallback_model="explicit-fallback-sol",
        )

    # Must produce the explicit fallback signal
    assert is_fallback is True
    assert decision.model_id == "explicit-fallback-sol"
    assert "explicit fallback to explicit-fallback-sol" in decision.reason
    assert "capability=coding" in decision.reason
    assert decision.fallbacks == []
    assert decision.estimated_cost_per_1k_in == 0.0

    # Warning logged - never silent
    assert any("cannot satisfy capability 'coding'" in r.message for r in caplog.records)


@db_test
def test_5_decision_trace_records_capability_and_reason(db_session_factory, tmp_path):
    """5. The decision trace records the capability and the reason."""
    catalog = _make_multi_capability_catalog()
    router = ModelRouter(catalog, PolicyResolver())
    spy_client = SpyChatClient()

    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path / "artifacts",
        llm_client=spy_client,
        model_router=router,
    )

    # 1. Run research task (derived capability: reasoning)
    res_research = slice_.run("research strategies for database sharding")
    assert res_research.error is None
    assert res_research.execution_status == "completed"

    with db_session_factory() as session:
        traces_research = TraceStore(session).list_for_execution(res_research.execution_id)

    model_traces_research = [
        t for t in traces_research if t.decision_type is DecisionType.MODEL_SELECTION
    ]
    assert len(model_traces_research) >= 1
    trace_r = model_traces_research[0]
    assert trace_r.chosen == "deep-reasoner"
    assert "deep-reasoner" in trace_r.rationale
    assert trace_r.inputs_summary.get("capability") == "reasoning"
    assert trace_r.inputs_summary.get("fallback") is False
    assert trace_r.confidence == 1.0

    # 2. Run task explicitly requesting summarisation (derived capability: fast)
    res_fast = slice_.run("research: summarize distributed locking mechanisms")
    assert res_fast.error is None
    assert res_fast.execution_status == "completed"

    with db_session_factory() as session:
        traces_fast = TraceStore(session).list_for_execution(res_fast.execution_id)

    model_traces_fast = [
        t for t in traces_fast if t.decision_type is DecisionType.MODEL_SELECTION
    ]
    assert len(model_traces_fast) >= 1
    trace_f = model_traces_fast[0]
    assert trace_f.chosen == "flash-summarizer"
    assert "flash-summarizer" in trace_f.rationale
    assert trace_f.inputs_summary.get("capability") == "fast"
    assert trace_f.inputs_summary.get("fallback") is False
    assert trace_f.confidence == 1.0


def test_6_uap_model_catalog_override_honoured_end_to_end(monkeypatch):
    """6. UAP_MODEL_CATALOG override is honoured end to end."""
    monkeypatch.setenv(
        "UAP_MODEL_CATALOG",
        "custom-gw/override-alpha,custom-gw/override-beta",
    )
    catalog = ModelCatalog.default()
    assert {m.id for m in catalog.all()} == {
        "custom-gw/override-alpha",
        "custom-gw/override-beta",
    }

    router = ModelRouter(catalog, PolicyResolver())
    spy_client = SpyChatClient()

    # Research request should select from overridden catalog
    cap = capability_for(Domain.RESEARCH, "evaluate architectural trade-offs")
    decision, is_fb = router.select_or_fallback(RoutingRequest(capability=cap))
    assert not is_fb
    assert decision.model_id in {"custom-gw/override-alpha", "custom-gw/override-beta"}

    # Synthesizer execution verifies end-to-end model invocation
    synth = _llm_synthesizer(spy_client, router=router)
    asyncio.run(synth("question", [{"claim": "evidence"}]))
    assert len(spy_client.calls) == 1
    assert spy_client.calls[0]["model"] in {
        "custom-gw/override-alpha",
        "custom-gw/override-beta",
    }


def test_7_coding_domain_routes_to_coding_model():
    """7. Coding domain maps to CODING capability and selects a coding model."""
    catalog = _make_multi_capability_catalog()
    router = ModelRouter(catalog, PolicyResolver())

    task = TaskSpec(
        domain=Domain.CODING,
        goal="implement merge sort in python",
    )
    cap = capability_for(task.domain, task.goal)
    assert cap == ModelCapability.CODING

    decision, is_fb = router.select_or_fallback(RoutingRequest(capability=cap))
    assert not is_fb
    assert decision.model_id == "code-specialist"
    assert "coding" in decision.reason


def test_8_taskspec_passed_directly_to_capability_for():
    """8. TaskSpec object passed directly to capability_for is properly parsed."""
    task_research = TaskSpec(domain=Domain.RESEARCH, goal="literature survey")
    assert capability_for(task_research) == ModelCapability.REASONING

    task_fast = TaskSpec(
        domain=Domain.RESEARCH,
        goal="literature survey",
        constraints={"fast": True},
    )
    assert capability_for(task_fast) == ModelCapability.FAST

    task_coding = TaskSpec(domain=Domain.CODING, goal="refactor ast")
    assert capability_for(task_coding) == ModelCapability.CODING
