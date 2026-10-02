"""Error recovery (Master section 35).

Two cohesive stages, injectable and deterministic:

* :class:`ErrorClassifier` -> :class:`ErrorClass`  (classify a failure)
* :class:`RecoveryPolicy` -> :class:`RecoveryDecision`  (decide retry / fallback /
  reroute / ask / fail)

The policy is decision-only: it computes the backoff but never sleeps, so the
caller owns the retry loop and the recovery decision stays auditable (section
35).
"""

from __future__ import annotations

from .classifier import ErrorClass, ErrorClassifier
from .policy import RecoveryAction, RecoveryDecision, RecoveryPolicy

__all__ = [
    "ErrorClass",
    "ErrorClassifier",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryPolicy",
]
