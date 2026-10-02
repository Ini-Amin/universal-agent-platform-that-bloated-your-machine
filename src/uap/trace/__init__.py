"""Decision trace package (Master section 26).

Public surface:

* :class:`DecisionType` / :class:`DecisionTrace` (and its
  :class:`DecisionAlternative` / :class:`DecisionEvidence` parts) - the
  structured operational-rationale contract.
* :class:`TraceStore` - session-injected repository over ``decision_traces``.
* :class:`DecisionRecorder` - persists a trace and emits the canonical
  ``DECISION_RECORDED`` event.
* :func:`redact_secrets` - the redaction guard applied before persistence.
"""

from __future__ import annotations

from uap.trace.model import (
    DecisionAlternative,
    DecisionEvidence,
    DecisionTrace,
    DecisionType,
)
from uap.trace.store import DecisionRecorder, TraceStore, redact_secrets

__all__ = [
    "DecisionAlternative",
    "DecisionEvidence",
    "DecisionRecorder",
    "DecisionTrace",
    "DecisionType",
    "TraceStore",
    "redact_secrets",
]
