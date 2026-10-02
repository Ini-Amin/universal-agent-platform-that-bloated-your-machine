"""Budget tracking and enforcement (Master section 34).

:class:`BudgetTracker` accumulates token/cost usage for one execution scope and
answers the pre-call question ``check() -> (within_budget, reason)``. It is
pure bookkeeping: deterministic, offline, no model calls, no I/O.

A plain class (not a Pydantic model) is used deliberately: counters are hot
mutable state and the tracker must stay trivially serialisable/inspectable.
"""

from __future__ import annotations

from uap.models.policy import BudgetPolicy

__all__ = ["BudgetExceededError", "BudgetTracker"]


class BudgetExceededError(Exception):
    """Raised by :meth:`BudgetTracker.check_or_raise` when the budget is spent."""


class BudgetTracker:
    """Accumulates usage and enforces a :class:`BudgetPolicy`.

    ``record`` only ever grows the counters; ``check`` compares them against the
    policy's optional ceilings. A ``None`` ceiling means "unbounded".
    """

    def __init__(self, policy: BudgetPolicy) -> None:
        self.policy = policy
        self._tokens_used = 0
        self._cost_used = 0.0
        self._calls = 0
        self._per_model: dict[str, dict[str, float]] = {}

    def record(self, model_id: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        """Add one call's usage to the running totals."""
        if tokens_in < 0 or tokens_out < 0:
            raise ValueError("token counts must be non-negative")
        if cost < 0:
            raise ValueError("cost must be non-negative")
        self._tokens_used += tokens_in + tokens_out
        self._cost_used += cost
        self._calls += 1
        entry = self._per_model.setdefault(
            model_id, {"tokens": 0.0, "cost": 0.0, "calls": 0.0}
        )
        entry["tokens"] += tokens_in + tokens_out
        entry["cost"] += cost
        entry["calls"] += 1

    def check(self) -> tuple[bool, str]:
        """Return ``(within_budget, reason)``; call this BEFORE each model call."""
        if (
            self.policy.max_tokens_total is not None
            and self._tokens_used > self.policy.max_tokens_total
        ):
            return (
                False,
                f"token budget exceeded: {self._tokens_used} > "
                f"{self.policy.max_tokens_total}",
            )
        if (
            self.policy.max_cost_total is not None
            and self._cost_used > self.policy.max_cost_total
        ):
            return (
                False,
                f"cost budget exceeded: {self._cost_used:.6f} > "
                f"{self.policy.max_cost_total:.6f}",
            )
        return (True, "within budget")

    def check_or_raise(self) -> None:
        """Raise :class:`BudgetExceededError` when :meth:`check` fails."""
        ok, reason = self.check()
        if not ok:
            raise BudgetExceededError(reason)

    def remaining(self) -> dict:
        """Remaining allowance; ``None`` for unbounded dimensions."""
        tokens = (
            None
            if self.policy.max_tokens_total is None
            else self.policy.max_tokens_total - self._tokens_used
        )
        cost = (
            None
            if self.policy.max_cost_total is None
            else self.policy.max_cost_total - self._cost_used
        )
        return {"tokens": tokens, "cost": cost}

    def exceeded(self) -> bool:
        """True when :meth:`check` would reject."""
        return not self.check()[0]

    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    @property
    def cost_used(self) -> float:
        return self._cost_used

    @property
    def calls(self) -> int:
        return self._calls

    def usage_by_model(self) -> dict[str, dict[str, float]]:
        """Per-model usage breakdown (copy), for traces and reporting."""
        return {mid: dict(vals) for mid, vals in self._per_model.items()}
