"""Model & budget policy with layered precedence (Master sections 33, 34).

A :class:`BudgetPolicy` is a *partial* declaration: every field is optional and
``None`` (or an empty list) means "no opinion, inherit". :class:`PolicyResolver`
folds a stack of partial policies into one effective policy.

Precedence (highest first), per Master section 33:

    runtime override > agent > workflow > workspace > user > system > system default

Merge semantics:

* scalar fields - the highest-precedence non-None value wins;
* ``blocked_models`` / ``fallback_chain`` - concatenated highest-precedence
  first, de-duplicated preserving that order;
* ``allowed_models`` - highest-precedence non-None wins, so a stricter inner
  layer narrows (never widens) an outer allow-list.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["BudgetPolicy", "PolicyResolver"]

#: Scalar fields: first non-None value in precedence order wins.
_SCALAR_FIELDS = (
    "max_tokens_total",
    "max_cost_total",
    "max_tokens_per_call",
    "escalation_threshold",
)

#: List fields: concatenated highest-precedence first, then de-duplicated.
_LIST_FIELDS = ("blocked_models", "fallback_chain")


class BudgetPolicy(BaseModel):
    """Partial model/budget policy; unset fields inherit from lower layers.

    ``extra="forbid"`` so a misspelled policy key fails loudly rather than
    being ignored - silent policy drift is a security problem.
    """

    model_config = ConfigDict(extra="forbid")

    max_tokens_total: int | None = None
    max_cost_total: float | None = None
    max_tokens_per_call: int | None = None
    allowed_models: list[str] | None = None  # None = all models allowed
    blocked_models: list[str] = Field(default_factory=list)
    fallback_chain: list[str] = Field(default_factory=list)  # ordered model ids
    escalation_threshold: float | None = None
    # NOTE: escalation is intentionally kept simple - the fallback chain *is*
    # the escalation path. A future layer may add retry-count triggers.


class PolicyResolver:
    """Folds layered :class:`BudgetPolicy` objects into one effective policy."""

    def __init__(self, system_default: BudgetPolicy | None = None) -> None:
        self.system_default = (
            system_default if system_default is not None else BudgetPolicy()
        )

    def resolve(
        self,
        *,
        system: BudgetPolicy | None = None,
        user: BudgetPolicy | None = None,
        workspace: BudgetPolicy | None = None,
        workflow: BudgetPolicy | None = None,
        agent: BudgetPolicy | None = None,
        runtime: BudgetPolicy | None = None,
    ) -> BudgetPolicy:
        """Return the effective policy for one model call.

        Only non-None layers participate; the constructor's
        ``system_default`` is always the lowest layer.
        """
        layers = [
            layer
            for layer in (runtime, agent, workflow, workspace, user, system,
                          self.system_default)
            if layer is not None
        ]

        merged: dict[str, object] = {}
        for field_name in _SCALAR_FIELDS:
            for layer in layers:
                value = getattr(layer, field_name)
                if value is not None:
                    merged[field_name] = value
                    break

        # allowed_models: first non-None wins (inner layer narrows outer).
        for layer in layers:
            if layer.allowed_models is not None:
                merged["allowed_models"] = list(layer.allowed_models)
                break

        for field_name in _LIST_FIELDS:
            merged[field_name] = self._concat_dedup(layers, field_name)

        return BudgetPolicy(**merged)

    @staticmethod
    def _concat_dedup(layers: list[BudgetPolicy], field_name: str) -> list[str]:
        """Concatenate ``field_name`` across layers, keeping first occurrence."""
        seen: set[str] = set()
        out: list[str] = []
        for layer in layers:
            for value in getattr(layer, field_name):
                if value not in seen:
                    seen.add(value)
                    out.append(value)
        return out
