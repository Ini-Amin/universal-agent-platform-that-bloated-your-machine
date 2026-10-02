"""Deterministic tool policy guard (Master sections 9 and 20).

Policy lives outside LLM reasoning: the model proposes a tool call, this guard
decides whether it may run. `check` is a pure function of (ToolSpec,
ApprovalRequest | None) with no side effects, so it can be unit-tested
exhaustively and never depends on how the tool was implemented.

Precedence inside `check`:
1. tiers listed in `require_approval_tiers` run only with an APPROVED request;
   a missing or pending request yields `needs_approval=True`, a rejected one
   is a hard deny;
2. tiers in `allowed_tiers` are auto-allowed;
3. every other tier is blocked as outside the allowed set.

Risk tier vocabulary (docs/research/bugbounty-mcp-inventory.md section 4):
0 pure local, 1 passive external, 2 active external, 3 side-effectful.
"""

from __future__ import annotations

from dataclasses import dataclass

from uap.contracts import ApprovalRequest, ApprovalState
from uap.tools.registry import RISK_TIERS, ToolSpec

DEFAULT_ALLOWED_TIERS: frozenset[int] = frozenset({0, 1, 2})
DEFAULT_REQUIRE_APPROVAL_TIERS: frozenset[int] = frozenset({3})


@dataclass(frozen=True)
class PolicyDecision:
    """The verdict a caller must honor: may it run now, or must a human decide?"""

    allowed: bool
    needs_approval: bool
    reason: str


class ToolPolicy:
    """Tier-based allow/deny with an explicit human gate for side effects."""

    def __init__(
        self,
        allowed_tiers: set[int] | None = None,
        require_approval_tiers: set[int] | None = None,
    ) -> None:
        self.allowed_tiers = (
            set(allowed_tiers) if allowed_tiers is not None else set(DEFAULT_ALLOWED_TIERS)
        )
        self.require_approval_tiers = (
            set(require_approval_tiers)
            if require_approval_tiers is not None
            else set(DEFAULT_REQUIRE_APPROVAL_TIERS)
        )

    def check(self, spec: ToolSpec, approval: ApprovalRequest | None) -> PolicyDecision:
        if spec.risk_tier in self.require_approval_tiers:
            return self._check_approval(spec, approval)

        if spec.risk_tier in self.allowed_tiers:
            return PolicyDecision(
                allowed=True,
                needs_approval=False,
                reason=(
                    f"risk tier {spec.risk_tier} ({spec.risk_label}) is allowed by "
                    "policy; no approval required"
                ),
            )

        return PolicyDecision(
            allowed=False,
            needs_approval=False,
            reason=(
                f"risk tier {spec.risk_tier} ({spec.risk_label}) is outside the "
                f"allowed tiers {sorted(self.allowed_tiers)}"
            ),
        )

    def _check_approval(
        self, spec: ToolSpec, approval: ApprovalRequest | None
    ) -> PolicyDecision:
        if approval is None:
            return PolicyDecision(
                allowed=False,
                needs_approval=True,
                reason=(
                    f"risk tier {spec.risk_tier} ({spec.risk_label}) requires an "
                    "approved request; none was provided"
                ),
            )

        if approval.state == ApprovalState.APPROVED:
            by = f" by {approval.decided_by}" if approval.decided_by else ""
            return PolicyDecision(
                allowed=True,
                needs_approval=False,
                reason=f"risk tier {spec.risk_tier} ({spec.risk_label}) approved{by}",
            )

        return PolicyDecision(
            allowed=False,
            needs_approval=approval.state == ApprovalState.PENDING_APPROVAL,
            reason=(
                f"risk tier {spec.risk_tier} ({spec.risk_label}) requires an approved "
                f"request; current state: {approval.state.value}"
            ),
        )
