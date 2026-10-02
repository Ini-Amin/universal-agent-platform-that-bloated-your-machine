"""Tests for the global Model Router + budget policy (Master sections 33, 34).

Deterministic and offline: the catalog is data and selection is pure ranking.
Cost-sensitive behaviour is exercised with a synthetic catalog because the
approved default catalog is all-zero-cost (local subscription).
"""

from __future__ import annotations

import pytest

from uap.contracts import TokenUsage
from uap.models import (
    BudgetExceededError,
    BudgetPolicy,
    BudgetTracker,
    ModelCapability,
    ModelCatalog,
    ModelInfo,
    ModelRouter,
    PolicyResolver,
    RoutingDecision,
    RoutingError,
    RoutingRequest,
)

APPROVED = {
    "gpt-5.6-sol",
    "deepseek-v4.1-flash",
    "gemini-3.8-flash-high",
    "local-reasoning-preview",
    "local-fast-preview",
}


def _info(
    model_id: str,
    *,
    capabilities: list[ModelCapability],
    cost_in: float = 0.0,
    cost_out: float = 0.0,
    latency: str = "standard",
    context_window: int = 128_000,
    available: bool = True,
) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        provider=model_id.split("/", 1)[0],
        capabilities=capabilities,
        context_window=context_window,
        cost_per_1k_in=cost_in,
        cost_per_1k_out=cost_out,
        latency_class=latency,
        available=available,
    )


def _cost_catalog() -> ModelCatalog:
    """Three FAST models with distinct cost/latency for ranking tests."""
    return ModelCatalog(
        [
            _info("p/cheap", capabilities=[ModelCapability.FAST], cost_in=0.10, latency="slow"),
            _info("p/mid", capabilities=[ModelCapability.FAST], cost_in=0.30, latency="standard"),
            _info("p/quick", capabilities=[ModelCapability.FAST], cost_in=0.50, latency="fast"),
            _info("p/reason", capabilities=[ModelCapability.REASONING], cost_in=0.05),
        ]
    )


# 1 -------------------------------------------------------------------------
def test_default_catalog_has_five_approved_models() -> None:
    """The built-in starter catalog: 5 capability-tagged models.

    Ids are generic (no vendor/proxy names) so the platform works against any
    OpenAI-compatible gateway; override with UAP_MODEL_CATALOG.
    """
    catalog = ModelCatalog.default()
    ids = {m.id for m in catalog.all()}
    assert ids == APPROVED
    assert len(catalog.all()) == 5
    for info in catalog.all():
        assert info.capabilities, f"{info.id} has no capability tag"


# 2 -------------------------------------------------------------------------
def test_duplicate_register_raises_value_error() -> None:
    catalog = ModelCatalog()
    info = _info("p/x", capabilities=[ModelCapability.FAST])
    catalog.register(info)
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(info)


# 3 -------------------------------------------------------------------------
def test_by_capability_deterministic_ordering() -> None:
    catalog = ModelCatalog(
        [
            _info("p/zeta", capabilities=[ModelCapability.CODING], cost_in=0.20),
            _info("p/alpha", capabilities=[ModelCapability.CODING], cost_in=0.20),
            _info("p/low", capabilities=[ModelCapability.CODING], cost_in=0.05),
            _info("p/other", capabilities=[ModelCapability.FAST], cost_in=0.01),
        ]
    )
    ordered = [m.id for m in catalog.by_capability(ModelCapability.CODING)]
    assert ordered == ["p/low", "p/alpha", "p/zeta"]  # cost asc, then id asc
    assert [m.id for m in catalog.by_capability(ModelCapability.EMBEDDING)] == []


# 4 -------------------------------------------------------------------------
def test_select_picks_cheapest_capable_model_under_prefer_cost() -> None:
    router = ModelRouter(_cost_catalog())
    decision = router.select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="cost")
    )
    assert decision.model_id == "p/cheap"
    assert decision.estimated_cost_per_1k_in == 0.10


# 5 -------------------------------------------------------------------------
def test_select_prefers_fast_latency_under_prefer_latency() -> None:
    router = ModelRouter(_cost_catalog())
    decision = router.select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="latency")
    )
    assert decision.model_id == "p/quick"  # only fast latency_class


# 6 -------------------------------------------------------------------------
def test_select_respects_allowed_models_narrowing() -> None:
    router = ModelRouter(_cost_catalog())
    policy = BudgetPolicy(allowed_models=["p/mid", "p/quick"])
    decision = router.select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="cost", policy=policy)
    )
    assert decision.model_id == "p/mid"


# 7 -------------------------------------------------------------------------
def test_blocked_models_excluded() -> None:
    router = ModelRouter(_cost_catalog())
    policy = BudgetPolicy(blocked_models=["p/cheap"])
    decision = router.select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="cost", policy=policy)
    )
    assert decision.model_id == "p/mid"


# 8 -------------------------------------------------------------------------
def test_exclude_list_excluded() -> None:
    router = ModelRouter(_cost_catalog())
    decision = router.select(
        RoutingRequest(
            capability=ModelCapability.FAST,
            prefer="cost",
            exclude=["p/cheap", "p/mid"],
        )
    )
    assert decision.model_id == "p/quick"


# 9 -------------------------------------------------------------------------
def test_context_tokens_needed_filters_small_windows() -> None:
    catalog = ModelCatalog(
        [
            _info("p/tiny", capabilities=[ModelCapability.REASONING], context_window=8_000, cost_in=0.01),
            _info("p/big", capabilities=[ModelCapability.REASONING], context_window=200_000, cost_in=0.90),
        ]
    )
    router = ModelRouter(catalog)
    decision = router.select(
        RoutingRequest(
            capability=ModelCapability.REASONING,
            context_tokens_needed=100_000,
            prefer="cost",
        )
    )
    assert decision.model_id == "p/big"


# 10 ------------------------------------------------------------------------
def test_fallbacks_ordered_and_exclude_chosen_and_dupes() -> None:
    router = ModelRouter(_cost_catalog())
    policy = BudgetPolicy(fallback_chain=["p/mid", "p/extra"])
    decision = router.select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="cost", policy=policy)
    )
    assert decision.model_id == "p/cheap"
    # remaining candidates (cost asc) then chain, chosen removed, dupes dropped
    assert decision.fallbacks == ["p/mid", "p/quick", "p/extra"]


# 11 ------------------------------------------------------------------------
def test_no_candidate_raises_routing_error_with_reason() -> None:
    router = ModelRouter(ModelCatalog.default())
    with pytest.raises(RoutingError) as excinfo:
        router.select(RoutingRequest(capability=ModelCapability.EMBEDDING))
    assert "capability=embedding" in str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)


# 12 ------------------------------------------------------------------------
def test_resolver_scalar_precedence_runtime_over_agent_over_workflow() -> None:
    resolver = PolicyResolver()
    resolved = resolver.resolve(
        workflow=BudgetPolicy(max_tokens_total=100),
        agent=BudgetPolicy(max_tokens_total=200),
        runtime=BudgetPolicy(max_tokens_total=300),
    )
    assert resolved.max_tokens_total == 300
    # dropping the runtime layer exposes the agent value, then workflow
    assert resolver.resolve(
        workflow=BudgetPolicy(max_tokens_total=100),
        agent=BudgetPolicy(max_tokens_total=200),
    ).max_tokens_total == 200
    assert resolver.resolve(
        workflow=BudgetPolicy(max_tokens_total=100)
    ).max_tokens_total == 100


# 13 ------------------------------------------------------------------------
def test_resolver_list_concat_order_and_dedup() -> None:
    resolver = PolicyResolver()
    resolved = resolver.resolve(
        workflow=BudgetPolicy(blocked_models=["c"], fallback_chain=["f"]),
        agent=BudgetPolicy(blocked_models=["a", "b"], fallback_chain=["g", "f"]),
        runtime=BudgetPolicy(blocked_models=["a"]),
    )
    assert resolved.blocked_models == ["a", "b", "c"]
    assert resolved.fallback_chain == ["g", "f"]  # runtime empty, agent first, dedup


# 14 ------------------------------------------------------------------------
def test_allowed_models_narrowing_across_layers() -> None:
    resolver = PolicyResolver()
    resolved = resolver.resolve(
        system=BudgetPolicy(allowed_models=["x", "y", "z"]),
        agent=BudgetPolicy(allowed_models=["x"]),
    )
    assert resolved.allowed_models == ["x"]
    # a layer with an explicit empty allow-list narrows to nothing
    assert resolver.resolve(
        system=BudgetPolicy(allowed_models=["x", "y"]),
        agent=BudgetPolicy(allowed_models=[]),
    ).allowed_models == []


# 15 ------------------------------------------------------------------------
def test_budget_tracker_within_then_exceeded_tokens() -> None:
    tracker = BudgetTracker(BudgetPolicy(max_tokens_total=100))
    tracker.record("p/a", 40, 20, 0.0)
    assert tracker.check() == (True, "within budget")
    tracker.record("p/a", 30, 20, 0.0)  # total 110 > 100
    ok, reason = tracker.check()
    assert ok is False
    assert "token budget exceeded" in reason
    assert tracker.exceeded() is True
    assert tracker.remaining() == {"tokens": -10, "cost": None}


# 16 ------------------------------------------------------------------------
def test_budget_exceeded_error_raised_by_check_or_raise() -> None:
    tracker = BudgetTracker(BudgetPolicy(max_cost_total=1.0))
    tracker.record("p/a", 0, 0, 2.5)
    with pytest.raises(BudgetExceededError, match="cost budget exceeded"):
        tracker.check_or_raise()


# 17 ------------------------------------------------------------------------
def test_record_usage_accumulates_on_router_tracker() -> None:
    router = ModelRouter(_cost_catalog())
    router.record_usage(
        "p/mid", TokenUsage(tokens_in=1_000, tokens_out=500, latency_ms=12.0)
    )
    router.record_usage("p/mid", TokenUsage(tokens_in=500, tokens_out=500))
    assert router.budget.tokens_used == 2_500
    assert router.budget.calls == 2
    # 1500 in * 0.30/1k + 1000 out * 0.0/1k == 0.45
    assert router.budget.cost_used == pytest.approx(0.45)
    assert router.budget.usage_by_model()["p/mid"]["calls"] == 2


# 18 ------------------------------------------------------------------------
def test_estimate_cost_math_exact() -> None:
    catalog = ModelCatalog(
        [
            _info(
                "p/pricey",
                capabilities=[ModelCapability.REASONING],
                cost_in=2.0,
                cost_out=4.0,
            )
        ]
    )
    router = ModelRouter(catalog)
    assert router.estimate_cost("p/pricey", 1_500, 250) == pytest.approx(4.0)
    assert router.estimate_cost("p/pricey", 0, 0) == 0.0
    with pytest.raises(ValueError, match="unknown model"):
        router.estimate_cost("p/missing", 1, 1)


# 19 ------------------------------------------------------------------------
def test_reason_strings_mention_deciding_factor() -> None:
    router = ModelRouter(_cost_catalog())
    cost_reason = router.select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="cost")
    ).reason
    latency_reason = router.select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="latency")
    ).reason
    quality_reason = router.select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="quality")
    ).reason
    assert "cheapest" in cost_reason
    assert "prefer=cost" in cost_reason
    assert "latency" in latency_reason
    assert "prefer=latency" in latency_reason
    assert "quality" in quality_reason


# 20 (extra) -----------------------------------------------------------------
def test_select_returns_decision_with_applied_policy_and_capability_filter() -> None:
    router = ModelRouter(_cost_catalog())
    decision = router.select(
        RoutingRequest(capability=ModelCapability.REASONING, prefer="cost")
    )
    assert isinstance(decision, RoutingDecision)
    assert decision.model_id == "p/reason"
    assert isinstance(decision.policy_applied, BudgetPolicy)


# 21 (extra) -----------------------------------------------------------------
def test_resolver_defaults_and_request_policy_blocked_via_router() -> None:
    resolver = PolicyResolver(BudgetPolicy(max_tokens_per_call=50))
    assert resolver.resolve().max_tokens_per_call == 50
    router = ModelRouter(_cost_catalog(), resolver)
    decision = router.select(
        RoutingRequest(
            capability=ModelCapability.FAST,
            prefer="cost",
            policy=BudgetPolicy(blocked_models=["p/cheap", "p/mid"]),
        )
    )
    assert decision.model_id == "p/quick"
    assert decision.policy_applied.max_tokens_per_call == 50


# 22 (extra) -----------------------------------------------------------------
def test_unavailable_models_are_skipped() -> None:
    catalog = ModelCatalog(
        [
            _info("p/down", capabilities=[ModelCapability.FAST], cost_in=0.01, available=False),
            _info("p/up", capabilities=[ModelCapability.FAST], cost_in=0.90),
        ]
    )
    decision = ModelRouter(catalog).select(
        RoutingRequest(capability=ModelCapability.FAST, prefer="cost")
    )
    assert decision.model_id == "p/up"
