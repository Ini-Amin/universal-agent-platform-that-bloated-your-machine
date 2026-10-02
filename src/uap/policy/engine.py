"""General-purpose Allow/Ask/Deny policy engine (Master section 31).

Where :mod:`uap.tools.policy` gates tool *risk tiers*, this engine evaluates
arbitrary ``(subject, action, resource)`` triples against ordered rules and
emits one of three effects. It is the "Approval Boundary" of section 31 and the
policy stage of the section-29 capability pipeline.

Determinism (fully specified so ties never depend on dict order):

1. only rules whose three fnmatch patterns all match are candidates;
2. the candidate with the **highest priority** wins;
3. ties break by effect severity ``DENY > ASK > ALLOW``;
4. remaining ties break by ``rule.id`` ascending.

No candidate -> ``(ASK, "no rule matched")`` -- fail closed to human review,
never to silent allow (section 68).
"""

from __future__ import annotations

from enum import StrEnum
from fnmatch import fnmatchcase

from pydantic import BaseModel, ConfigDict

__all__ = ["PolicyEffect", "PolicyRule", "PolicyEngine"]


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


# Higher = stronger when two rules share a priority.
_SEVERITY: dict[PolicyEffect, int] = {
    PolicyEffect.DENY: 2,
    PolicyEffect.ASK: 1,
    PolicyEffect.ALLOW: 0,
}


class PolicyRule(BaseModel):
    """One ``(subject, action, resource)`` -> effect rule with fnmatch globs."""

    model_config = ConfigDict(extra="forbid")

    id: str
    effect: PolicyEffect
    priority: int = 0
    subject_pattern: str = "*"
    action_pattern: str = "*"
    resource_pattern: str = "*"
    reason: str = ""

    def matches(self, subject: str, action: str, resource: str) -> bool:
        return (
            fnmatchcase(subject, self.subject_pattern)
            and fnmatchcase(action, self.action_pattern)
            and fnmatchcase(resource, self.resource_pattern)
        )


class PolicyEngine:
    """Ordered rule set with a deterministic, fail-closed decision function."""

    def __init__(self, rules: list[PolicyRule]) -> None:
        self._rules = list(rules)

    def decide(
        self,
        subject: str,
        action: str,
        resource: str,
        context: dict | None = None,
    ) -> tuple[PolicyEffect, str]:
        """Return ``(effect, reason)`` per the module-level precedence rules."""
        candidates = [
            r for r in self._rules if r.matches(subject, action, resource)
        ]
        if not candidates:
            return PolicyEffect.ASK, "no rule matched"
        winner = min(
            candidates,
            key=lambda r: (-r.priority, -_SEVERITY[r.effect], r.id),
        )
        return winner.effect, winner.reason or f"matched rule {winner.id}"

    # ------------------------------------------------------------------ #
    # Default policy
    # ------------------------------------------------------------------ #

    @classmethod
    def default(cls) -> "PolicyEngine":
        """A sane fail-closed baseline (section 68 "never leak secrets")."""
        return cls(
            [
                # Secrets/credentials/env files are never readable, whatever the action.
                PolicyRule(
                    id="deny-secrets",
                    effect=PolicyEffect.DENY,
                    priority=100,
                    resource_pattern="*secret*",
                    reason="secret resources are never accessible",
                ),
                PolicyRule(
                    id="deny-credentials",
                    effect=PolicyEffect.DENY,
                    priority=100,
                    resource_pattern="*credential*",
                    reason="credential resources are never accessible",
                ),
                PolicyRule(
                    id="deny-dotenv",
                    effect=PolicyEffect.DENY,
                    priority=100,
                    resource_pattern="*.env*",
                    reason="env files are never accessible",
                ),
                # Side-effectful actions require human confirmation.
                PolicyRule(
                    id="ask-fs-write",
                    effect=PolicyEffect.ASK,
                    priority=50,
                    action_pattern="fs:write",
                    reason="filesystem writes require approval",
                ),
                PolicyRule(
                    id="ask-net-outbound",
                    effect=PolicyEffect.ASK,
                    priority=50,
                    action_pattern="net:outbound",
                    reason="outbound network requires approval",
                ),
                # Known-safe local reads/validations are auto-allowed.
                PolicyRule(
                    id="allow-fs-read",
                    effect=PolicyEffect.ALLOW,
                    priority=10,
                    action_pattern="fs:read",
                    reason="local reads are allowed",
                ),
                PolicyRule(
                    id="allow-validate-scope",
                    effect=PolicyEffect.ALLOW,
                    priority=10,
                    action_pattern="tool:validate_scope",
                    reason="scope validation is allowed",
                ),
            ]
        )
