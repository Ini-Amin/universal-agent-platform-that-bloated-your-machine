"""Decision trace model (Master section 26).

Section 26 mandates a **Full Decision Trace System** that stores *structured
operational rationale* - explicitly **never raw private chain-of-thought**.
This module defines that structure.

A :class:`DecisionTrace` answers the section 26 questions for one decision:

* **what** was decided - ``decision_type`` + ``chosen``.
* **what else** was considered - ``alternatives`` (option + why rejected).
* **why** - ``rationale`` (a *short structured reason*, not a reasoning dump).
* **on what basis** - ``evidence`` (artifact / event / file / scope rule refs).
* **how sure** - ``confidence`` (0..1).
* **with what inputs** - ``inputs_summary`` (small, redacted; never secrets).

Traces are keyed to ``execution_id`` (and optionally ``node_id``) so they can be
joined to the canonical event stream (section 45/46) and to checkpoints.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uap.contracts import UTCDateTime, utc_now

__all__ = [
    "DecisionAlternative",
    "DecisionEvidence",
    "DecisionTrace",
    "DecisionType",
]

class DecisionType(StrEnum):
    """The kinds of operational decision a runtime can record (section 26)."""

    ROUTING = "routing"
    MODEL_SELECTION = "model_selection"
    TOOL_SELECTION = "tool_selection"
    SCOPE_CHECK = "scope_check"
    POLICY_CHECK = "policy_check"
    APPROVAL = "approval"
    RETRY = "retry"
    FALLBACK = "fallback"
    REPLAN = "replan"
    CONDITION = "condition"
    SYNTHESIS = "synthesis"

class DecisionAlternative(BaseModel):
    """One option that was *not* chosen, with the reason it was rejected."""

    model_config = ConfigDict(extra="forbid")

    option: str
    reason_rejected: str

class DecisionEvidence(BaseModel):
    """A reference to something that supported the decision.

    ``kind`` is an open label (``"artifact"``, ``"event"``, ``"file"``,
    ``"scope_rule"``, ...); ``ref`` is a stable identifier or path - never the
    raw evidence content.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    ref: str

class DecisionTrace(BaseModel):
    """A single structured operational decision (Master section 26).

    ``extra="forbid"`` keeps the record closed so a producer cannot smuggle
    unstructured reasoning onto the row via ad-hoc keys.
    """

    model_config = ConfigDict(extra="forbid")

    #: Stable id for this trace (also echoed into the DECISION_RECORDED event).
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_id: str
    node_id: str | None = None

    decision_type: DecisionType
    #: The chosen option (agent / tool / model / branch / verdict / ...).
    chosen: str
    #: Options considered and rejected, each with a short reason.
    alternatives: list[DecisionAlternative] = Field(default_factory=list)
    #: A SHORT structured reason - not chain-of-thought (section 26).
    rationale: str
    #: Evidence references that supported the decision.
    evidence: list[DecisionEvidence] = Field(default_factory=list)
    #: Confidence in the decision, when the producer can express it (0..1).
    confidence: float | None = None
    #: Small, redacted summary of the inputs - never secrets (section 73).
    inputs_summary: dict = Field(default_factory=dict)

    created_at: UTCDateTime = Field(default_factory=utc_now)

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, value: float | None) -> float | None:
        """Reject out-of-range confidence at the contract boundary."""

        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError("confidence must be within [0.0, 1.0]")
        return value
