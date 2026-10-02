"""Global model router (Master section 33).

Given a :class:`RoutingRequest` (capability + optional policy + preferences),
the router deterministically picks one model from the catalog and returns an
ordered fallback list. It never calls a provider: selection is pure ranking over
catalog data, so the same request always yields the same decision.

The rationale string returned in :class:`RoutingDecision` is the structured
"why" that callers attach to a Decision Trace as a MODEL_SELECTION entry
(Master section 26) - it names the deciding factor, never private reasoning.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import TokenUsage
from uap.models.budget import BudgetTracker
from uap.models.catalog import ModelCapability, ModelCatalog, ModelInfo
from uap.models.policy import BudgetPolicy, PolicyResolver

__all__ = [
    "RoutingRequest",
    "RoutingDecision",
    "RoutingError",
    "ModelRouter",
]

#: Lower rank = preferred when ``prefer="latency"``.
_LATENCY_RANK = {"fast": 0, "standard": 1, "slow": 2}

#: Lower rank = preferred when ``prefer="quality"`` (best capability wins).
_QUALITY_ORDER = (
    ModelCapability.REASONING,
    ModelCapability.CODING,
    ModelCapability.VISION,
    ModelCapability.FAST,
    ModelCapability.EMBEDDING,
)


class RoutingError(ValueError):
    """Raised when no model can satisfy a routing request.

    Subclasses :class:`ValueError` so callers that guard on the standard
    exception type still catch it.
    """


class RoutingRequest(BaseModel):
    """What the caller needs from a model (Master section 33)."""

    model_config = ConfigDict(extra="forbid")

    capability: ModelCapability
    context_tokens_needed: int = 0
    prefer: str | None = None  # "cost" | "latency" | "quality"
    policy: BudgetPolicy | None = None
    exclude: list[str] = Field(default_factory=list)


class RoutingDecision(BaseModel):
    """The chosen model plus the rationale and fallback path."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    reason: str  # structured rationale -> DecisionTrace MODEL_SELECTION
    fallbacks: list[str]  # ordered model ids to try if the primary fails
    estimated_cost_per_1k_in: float
    policy_applied: BudgetPolicy


class ModelRouter:
    """Provider-agnostic, deterministic model selector."""

    def __init__(
        self,
        catalog: ModelCatalog,
        resolver: PolicyResolver | None = None,
        *,
        usage_sink: Callable[[str, TokenUsage], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.resolver = resolver if resolver is not None else PolicyResolver()
        self.usage_sink = usage_sink
        self.budget = BudgetTracker(BudgetPolicy())

    def select(self, request: RoutingRequest) -> RoutingDecision:
        """Pick the best model for ``request`` or raise :class:`RoutingError`."""
        policy = self.resolver.resolve(runtime=request.policy)
        candidates = self._candidates(request, policy)
        if not candidates:
            raise RoutingError(self._no_candidate_reason(request, policy))

        prefer = request.prefer or "cost"
        ranked = sorted(candidates, key=lambda m: self._rank_key(m, prefer))
        chosen = ranked[0]
        fallbacks = self._fallbacks(ranked[1:], policy.fallback_chain, chosen.id)

        return RoutingDecision(
            model_id=chosen.id,
            reason=self._reason(chosen, request, prefer),
            fallbacks=fallbacks,
            estimated_cost_per_1k_in=chosen.cost_per_1k_in,
            policy_applied=policy,
        )

    def record_usage(self, model_id: str, usage: TokenUsage) -> None:
        """Feed one call's :class:`TokenUsage` into the router's tracker."""
        cost = self.estimate_cost(model_id, usage.tokens_in, usage.tokens_out)
        self.budget.record(model_id, usage.tokens_in, usage.tokens_out, cost)
        if self.usage_sink is not None:
            self.usage_sink(model_id, usage)

    def estimate_cost(self, model_id: str, tokens_in: int, tokens_out: int) -> float:
        """Exact cost for one call, from the model's per-1k rates."""
        info = self.catalog.get(model_id)
        if info is None:
            raise ValueError(f"unknown model: {model_id}")
        return (tokens_in / 1000.0) * info.cost_per_1k_in + (
            tokens_out / 1000.0
        ) * info.cost_per_1k_out

    # -- selection internals ------------------------------------------------

    def _candidates(
        self, request: RoutingRequest, policy: BudgetPolicy
    ) -> list[ModelInfo]:
        """Models passing every hard filter (capability, policy, context)."""
        excluded = set(request.exclude)
        blocked = set(policy.blocked_models)
        allowed = (
            set(policy.allowed_models) if policy.allowed_models is not None else None
        )
        out: list[ModelInfo] = []
        for info in self.catalog.all():
            if request.capability not in info.capabilities:
                continue
            if not info.available:
                continue
            if allowed is not None and info.id not in allowed:
                continue
            if info.id in blocked:
                continue
            if info.id in excluded:
                continue
            if info.context_window < request.context_tokens_needed:
                continue
            out.append(info)
        return out

    @staticmethod
    def _rank_key(info: ModelInfo, prefer: str) -> tuple:
        """Sort key implementing the three preference modes."""
        if prefer == "latency":
            return (
                _LATENCY_RANK.get(info.latency_class, _LATENCY_RANK["standard"]),
                info.cost_per_1k_in,
                info.id,
            )
        if prefer == "quality":
            return (ModelRouter._quality_rank(info), info.cost_per_1k_in, info.id)
        # default / "cost"
        return (info.cost_per_1k_in, info.cost_per_1k_out, info.id)

    @staticmethod
    def _quality_rank(info: ModelInfo) -> int:
        ranks = [
            _QUALITY_ORDER.index(c) for c in info.capabilities if c in _QUALITY_ORDER
        ]
        return min(ranks) if ranks else len(_QUALITY_ORDER)

    @staticmethod
    def _fallbacks(
        remaining: list[ModelInfo], chain: list[str], chosen_id: str
    ) -> list[str]:
        """Ranked remaining candidates, then the policy chain; dedup, no chosen."""
        out: list[str] = []
        seen: set[str] = {chosen_id}
        for info in remaining:
            if info.id not in seen:
                seen.add(info.id)
                out.append(info.id)
        for model_id in chain:
            if model_id not in seen:
                seen.add(model_id)
                out.append(model_id)
        return out

    @staticmethod
    def _reason(info: ModelInfo, request: RoutingRequest, prefer: str) -> str:
        capability = request.capability
        if prefer == "latency":
            return (
                f"selected {info.id} for {capability} with the fastest "
                f"latency_class={info.latency_class} (prefer=latency)"
            )
        if prefer == "quality":
            return (
                f"selected {info.id} for the strongest {capability} capability "
                f"match (prefer=quality)"
            )
        return (
            f"selected {info.id} as the cheapest {capability} model "
            f"(cost={info.cost_per_1k_in}/1k in, prefer=cost)"
        )

    @staticmethod
    def _no_candidate_reason(request: RoutingRequest, policy: BudgetPolicy) -> str:
        filters = [f"capability={request.capability}", "available=true"]
        if policy.allowed_models is not None:
            filters.append(f"allowed_models={policy.allowed_models}")
        if policy.blocked_models:
            filters.append(f"blocked_models={policy.blocked_models}")
        if request.exclude:
            filters.append(f"exclude={request.exclude}")
        if request.context_tokens_needed:
            filters.append(f"context_window>={request.context_tokens_needed}")
        return "no model satisfies " + ", ".join(filters)
