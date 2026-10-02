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

import re
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import Domain, TokenUsage
from uap.models.budget import BudgetTracker
from uap.models.catalog import ModelCapability, ModelCatalog, ModelInfo
from uap.models.policy import BudgetPolicy, PolicyResolver

__all__ = [
    "RoutingRequest",
    "RoutingDecision",
    "RoutingError",
    "ModelRouter",
    "DOMAIN_CAPABILITY_MAP",
    "DOMAIN_RATIONALE",
    "SPEED_KEYWORDS",
    "capability_for",
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

#: Documented domain to ModelCapability mapping table (Master section 33).
DOMAIN_CAPABILITY_MAP: dict[str, ModelCapability] = {
    Domain.RESEARCH.value: ModelCapability.REASONING,
    Domain.BBP.value: ModelCapability.REASONING,
    Domain.CODING.value: ModelCapability.CODING,
    Domain.LEARNING.value: ModelCapability.REASONING,
    Domain.DATA.value: ModelCapability.FAST,
    Domain.UNKNOWN.value: ModelCapability.REASONING,
}

#: Documented rationale explaining why each domain maps to its capability.
DOMAIN_RATIONALE: dict[str, str] = {
    Domain.RESEARCH.value: "research tasks require deep reasoning and evidence evaluation",
    Domain.BBP.value: "security recon requires analysis-heavy vulnerability assessment",
    Domain.CODING.value: "coding tasks require code generation, refactoring, and AST manipulation",
    Domain.LEARNING.value: "learning tasks require conceptual explanation and reasoning",
    Domain.DATA.value: "data analysis tasks favor fast transformation and summarization",
    Domain.UNKNOWN.value: "unknown domain defaults to general reasoning capability",
}

#: Keywords in goal/prompt indicating explicit request for speed or summarisation.
SPEED_KEYWORDS: frozenset[str] = frozenset({
    "fast",
    "speed",
    "quick",
    "rapid",
    "summarize",
    "summarise",
    "summary",
    "summarization",
    "summarisation",
    "latency",
})


def capability_for(
    domain: Any = None,
    goal: str | None = None,
    *,
    task: Any = None,
    constraints: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> ModelCapability:
    """Derive the required ModelCapability from domain, goal, or task context.

    Routing rules:
    1. Speed/Summarisation precedence: Any task explicitly requesting speed or
       summarisation (via keywords in the goal such as 'fast', 'quick', 'speed',
       'summarize', 'summary', or flags in constraints/extras like prefer='latency',
       fast=True, or domain='fast') maps to ModelCapability.FAST.
    2. Coding: Domain.CODING ('coding') maps to ModelCapability.CODING for code
       generation, refactoring, syntax analysis, and debugging.
    3. Research: Domain.RESEARCH ('research') maps to ModelCapability.REASONING
       for deep literature synthesis, trade-off analysis, and critical evaluation.
    4. Security Recon (BBP): Domain.BBP ('bbp') maps to ModelCapability.REASONING
       because security reconnaissance and vulnerability discovery are analysis-heavy.
    5. Other Domains: Domain.LEARNING maps to ModelCapability.REASONING; Domain.DATA
       maps to ModelCapability.FAST; unknown/unspecified domains default to
       ModelCapability.REASONING.

    Parameters:
        domain: Domain enum, string domain name, or TaskSpec instance.
        goal: Task goal string or prompt.
        task: Optional TaskSpec instance from which domain, goal, and constraints
            are extracted if not explicitly supplied.
        constraints: Optional constraint dictionary from TaskSpec.
        extras: Optional extras dictionary from AgentContext.

    Returns:
        ModelCapability: The capability to request from ModelRouter.
    """
    if hasattr(domain, "domain") and hasattr(domain, "goal"):
        task = domain
        domain = getattr(task, "domain", None)
        if goal is None:
            goal = getattr(task, "goal", None)
        if constraints is None:
            constraints = getattr(task, "constraints", None)

    if task is not None:
        if domain is None:
            domain = getattr(task, "domain", None)
        if goal is None:
            goal = getattr(task, "goal", None)
        if constraints is None:
            constraints = getattr(task, "constraints", None)

    norm_domain = ""
    if domain is not None:
        if hasattr(domain, "value"):
            norm_domain = str(domain.value).lower().strip()
        else:
            norm_domain = str(domain).lower().strip()
            if "." in norm_domain:
                norm_domain = norm_domain.split(".")[-1]

    # Explicit speed/summarisation domain request
    if norm_domain in ("fast", "speed", "quick", "summary", "summarize"):
        return ModelCapability.FAST

    # Constraints flags
    if constraints:
        if constraints.get("fast") is True or str(constraints.get("speed", "")).lower() in ("fast", "quick", "high"):
            return ModelCapability.FAST
        if constraints.get("prefer") == "latency" or str(constraints.get("latency", "")).lower() == "fast":
            return ModelCapability.FAST
        if constraints.get("summarize") or constraints.get("summary"):
            return ModelCapability.FAST

    # Extras flags
    if extras:
        if extras.get("fast") is True or str(extras.get("speed", "")).lower() in ("fast", "quick"):
            return ModelCapability.FAST
        if extras.get("prefer") == "latency" or str(extras.get("latency", "")).lower() == "fast":
            return ModelCapability.FAST
        if str(extras.get("capability", "")).lower() == "fast":
            return ModelCapability.FAST

    # Goal keywords
    if goal:
        words = set(re.findall(r"\b[a-zA-Z]+\b", goal.lower()))
        if bool(words & SPEED_KEYWORDS):
            return ModelCapability.FAST

    if norm_domain in DOMAIN_CAPABILITY_MAP:
        return DOMAIN_CAPABILITY_MAP[norm_domain]

    return ModelCapability.REASONING


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

    def select_or_fallback(
        self,
        request: RoutingRequest,
        fallback_model: str | None = None,
    ) -> tuple[RoutingDecision, bool]:
        """Pick the best model for ``request`` or fall back explicitly.

        Returns ``(decision, is_fallback)``. When ``is_fallback`` is True,
        logs a warning and returns an explicit fallback decision with the
        failure reason recorded in ``decision.reason``.
        """
        import logging
        import os

        fallback_id = (
            fallback_model
            or os.environ.get("UAP_DEFAULT_MODEL", "gpt-5.6-sol")
        )
        try:
            return self.select(request), False
        except RoutingError as exc:
            lg = logging.getLogger(__name__)
            lg.disabled = False
            lg.warning(
                "ModelRouter cannot satisfy capability %r: %s; explicitly falling back to %r",
                getattr(request.capability, "value", request.capability),
                exc,
                fallback_id,
            )
            policy = self.resolver.resolve(runtime=request.policy)
            decision = RoutingDecision(
                model_id=fallback_id,
                reason=f"explicit fallback to {fallback_id}: {exc}",
                fallbacks=[],
                estimated_cost_per_1k_in=0.0,
                policy_applied=policy,
            )
            return decision, True

    def select_or_fallback_for_domain(
        self,
        domain: Any = None,
        goal: str | None = None,
        *,
        fallback_model: str | None = None,
        task: Any = None,
        constraints: dict[str, Any] | None = None,
        extras: dict[str, Any] | None = None,
        request_overrides: dict[str, Any] | None = None,
    ) -> tuple[RoutingDecision, bool]:
        """Pick the best model for a domain/goal or fall back explicitly."""
        cap = capability_for(
            domain,
            goal,
            task=task,
            constraints=constraints,
            extras=extras,
        )
        req_kwargs: dict[str, Any] = {"capability": cap}
        if request_overrides:
            req_kwargs.update(request_overrides)
        return self.select_or_fallback(
            RoutingRequest(**req_kwargs),
            fallback_model=fallback_model,
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
