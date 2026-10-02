"""Error classification (Master section 35).

The first stage of the error-recovery pipeline (section 35):

    Node Failure -> Error Classifier -> Recovery Policy -> Retry/Fallback/...

:class:`ErrorClassifier` maps an exception (or a free-text error string) to an
:class:`ErrorClass` using deterministic, order-sensitive rules. Classification
never raises and never calls the network - it inspects the exception type and a
case-insensitive view of ``str(exc)``.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

__all__ = ["ErrorClass", "ErrorClassifier"]


class ErrorClass(StrEnum):
    """The recovery-relevant class of a failure (section 35)."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POLICY = "policy"
    RESOURCE = "resource"
    UNKNOWN = "unknown"


# Substring rules applied (case-insensitive) to ``str(exc)``. Order matters:
# the first class whose any-substring matches wins, so POLICY ("denied") is
# checked before PERMANENT ("not found") to avoid a sandbox denial being
# misread as a permanent miss.
_SUBSTRING_RULES: tuple[tuple[ErrorClass, tuple[str, ...]], ...] = (
    (ErrorClass.TRANSIENT, ("timed out", "timeout", "connection")),
    (ErrorClass.POLICY, ("denied", "forbidden", "not allowed", "blocked")),
    (ErrorClass.RESOURCE, ("quota", "budget", "exhausted")),
    (ErrorClass.PERMANENT, ("invalid", "unknown tool", "not found")),
)


class ErrorClassifier:
    """Deterministically classify a failure into an :class:`ErrorClass`."""

    def classify(self, exc: BaseException | str) -> ErrorClass:
        """Classify ``exc`` (an exception or an error string).

        Type-based rules run first (they are unambiguous), then
        case-insensitive substring rules on the message. Anything unmatched is
        :attr:`ErrorClass.UNKNOWN`.
        """

        if isinstance(exc, str):
            return self._classify_message(exc)

        # Type-based rules (checked before the message so a ``ValueError`` whose
        # text happens to contain "connection" is still PERMANENT by type).
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
            return ErrorClass.TRANSIENT
        if isinstance(exc, MemoryError):
            return ErrorClass.RESOURCE
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return ErrorClass.PERMANENT

        return self._classify_message(str(exc))

    @staticmethod
    def _classify_message(message: str) -> ErrorClass:
        lowered = message.lower()
        for error_class, needles in _SUBSTRING_RULES:
            if any(needle in lowered for needle in needles):
                return error_class
        return ErrorClass.UNKNOWN
