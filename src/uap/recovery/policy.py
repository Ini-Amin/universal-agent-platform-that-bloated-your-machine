"""Recovery policy (Master section 35).

The second stage of the recovery pipeline: given an :class:`ErrorClass` and the
current attempt count, :class:`RecoveryPolicy` returns a :class:`RecoveryDecision`
(retry / fallback / reroute / ask / fail). Decisions are *deterministic* and
pure - the policy computes the backoff delay but does not sleep. Recovery is
visible and auditable (section 35): the decision carries a human-readable
``reason``.

This policy is intentionally **decision-only**. The caller (worker / executor)
owns the actual retry loop and the sleep, so recovery stays testable without a
clock and the policy never blocks a run. ``delay_s`` is the computed backoff the
caller should wait before a RETRY.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .classifier import ErrorClass

__all__ = ["RecoveryAction", "RecoveryDecision", "RecoveryPolicy"]


class RecoveryAction(StrEnum):
    """What to do about a classified failure (section 35)."""

    RETRY = "retry"
    FALLBACK = "fallback"
    REROUTE = "reroute"
    ASK = "ask"
    FAIL = "fail"


class RecoveryDecision(BaseModel):
    """A single, auditable recovery decision (section 35)."""

    action: RecoveryAction
    reason: str
    #: Backoff the caller should wait before a RETRY (seconds); 0 otherwise.
    delay_s: float = 0.0


class RecoveryPolicy:
    """Maps ``(error_class, attempt)`` to a :class:`RecoveryDecision`.

    Decision table (section 35):

    ======================  ==============================================
    Error class             Decision
    ======================  ==============================================
    TRANSIENT (retriable)   RETRY with exponential backoff while
                            ``attempt <= max_retries``
    TRANSIENT (exhausted)   FALLBACK if available, else FAIL
    RESOURCE                ASK (human / budget decision)
    POLICY                  FAIL (policy denials are never retried)
    PERMANENT               REROUTE if an alternative exists, else FAIL
    UNKNOWN                 ASK (escalate the unclassifiable)
    ======================  ==============================================
    """

    def __init__(
        self,
        *,
        max_retries: int = 2,
        base_delay_s: float = 0.1,
        max_delay_s: float = 2.0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if base_delay_s < 0 or max_delay_s < 0:
            raise ValueError("delays must be >= 0")
        self.max_retries = max_retries
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s

    def decide(
        self,
        error_class: ErrorClass,
        *,
        attempt: int,
        has_fallback: bool = False,
        has_reroute: bool = False,
    ) -> RecoveryDecision:
        """Return the recovery decision for ``error_class`` at ``attempt``.

        ``attempt`` is the zero-based count of attempts already made (0 = the
        first failure). Only TRANSIENT failures consult ``attempt``; POLICY
        denials fail regardless of ``attempt`` (never retried).
        """

        if error_class is ErrorClass.TRANSIENT:
            if attempt < self.max_retries:
                return RecoveryDecision(
                    action=RecoveryAction.RETRY,
                    reason=(
                        f"transient failure, retry {attempt + 1}/{self.max_retries}"
                    ),
                    delay_s=self._backoff(attempt),
                )
            if has_fallback:
                return RecoveryDecision(
                    action=RecoveryAction.FALLBACK,
                    reason="transient retries exhausted; using fallback",
                )
            return RecoveryDecision(
                action=RecoveryAction.FAIL,
                reason="transient retries exhausted; no fallback",
            )

        if error_class is ErrorClass.RESOURCE:
            return RecoveryDecision(
                action=RecoveryAction.ASK,
                reason="resource exhaustion requires a human / budget decision",
            )

        if error_class is ErrorClass.POLICY:
            return RecoveryDecision(
                action=RecoveryAction.FAIL,
                reason="policy denial is never retried",
            )

        if error_class is ErrorClass.PERMANENT:
            if has_reroute:
                return RecoveryDecision(
                    action=RecoveryAction.REROUTE,
                    reason="permanent failure; rerouting to an alternative",
                )
            return RecoveryDecision(
                action=RecoveryAction.FAIL,
                reason="permanent failure; no reroute available",
            )

        return RecoveryDecision(
            action=RecoveryAction.ASK,
            reason="unclassified failure; escalating",
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff ``base * 2**attempt`` capped at ``max_delay_s``."""

        return min(self.base_delay_s * (2 ** attempt), self.max_delay_s)
