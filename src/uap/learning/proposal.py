"""Strategy proposals (Master section 48).

A :class:`StrategyProposal` is the audit-friendly record of a *proposed*
improvement produced by the closed-loop learner. Proposals are never
auto-applied: they carry an explicit lifecycle (``proposed`` -> ``approved`` /
``rejected`` -> ``applied``) so every permanent change has validation,
provenance and policy behind it (section 48).
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts.models import UTCDateTime, utc_now

__all__ = ["ProposalKind", "ProposalStatus", "StrategyProposal"]

_FORBID = ConfigDict(extra="forbid")


class ProposalKind(StrEnum):
    PROMPT_TWEAK = "prompt_tweak"
    WORKFLOW_CHANGE = "workflow_change"
    EVALUATOR_ADD = "evaluator_add"
    KNOWLEDGE_PROMOTE = "knowledge_promote"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


class StrategyProposal(BaseModel):
    """One proposed, policy-gated change to a versioned resource."""

    model_config = _FORBID

    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: ProposalKind
    target_ref: str
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)
    proposed_change: dict = Field(default_factory=dict)
    status: ProposalStatus = ProposalStatus.PROPOSED
    created_at: UTCDateTime = Field(default_factory=utc_now)
    decided_at: UTCDateTime | None = None
    decided_by: str | None = None
