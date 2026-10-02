"""Trust model and registry (Master section 25).

A result's trustworthiness follows the section 25 pipeline: *claim -> evidence
-> verification -> trust assessment*. The :class:`TrustRegistry` records a
:class:`TrustClaim`, and when a verifier marks it verified or failed it nudges
the producing agent's :class:`TrustRecord` score within ``[0.0, 1.0]``.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["TrustClaim", "TrustRecord", "TrustRegistry"]

_FORBID = ConfigDict(extra="forbid")

#: Score deltas and bounds for verification outcomes (section 25).
_VERIFIED_DELTA = 0.1
_FAILED_DELTA = -0.15
_FLOOR = 0.0
_CAP = 1.0
_INITIAL_SCORE = 0.5


class TrustClaim(BaseModel):
    """A statement an agent asserts, with references to supporting evidence."""

    model_config = _FORBID

    claim_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent: str
    statement: str
    evidence_refs: list[str] = Field(default_factory=list)


class TrustRecord(BaseModel):
    """A per-agent trust tally and bounded score."""

    model_config = _FORBID

    agent: str
    verified_claims: int = 0
    failed_claims: int = 0
    trust_score: float = _INITIAL_SCORE


class TrustRegistry:
    """Records claims and maintains bounded per-agent trust scores."""

    def __init__(self) -> None:
        self._claims: dict[str, TrustClaim] = {}
        self._records: dict[str, TrustRecord] = {}

    def claim(
        self, agent: str, statement: str, evidence_refs: list[str] | None = None
    ) -> TrustClaim:
        """Record a new claim by ``agent`` and ensure the agent has a record."""
        claim = TrustClaim(
            agent=agent,
            statement=statement,
            evidence_refs=list(evidence_refs or []),
        )
        self._claims[claim.claim_id] = claim
        self._ensure_record(agent)
        return claim

    def verify(self, claim_id: str, *, verified: bool) -> TrustRecord:
        """Mark ``claim_id`` verified or failed and update the agent's score.

        Verified claims add ``+0.1`` (capped at 1.0); failed claims subtract
        ``0.15`` (floored at 0.0). An unknown claim raises ``KeyError``.
        """
        if claim_id not in self._claims:
            raise KeyError(f"unknown claim: {claim_id!r}")
        claim = self._claims[claim_id]
        record = self._ensure_record(claim.agent)
        if verified:
            record.verified_claims += 1
            record.trust_score = min(_CAP, record.trust_score + _VERIFIED_DELTA)
        else:
            record.failed_claims += 1
            record.trust_score = max(_FLOOR, record.trust_score + _FAILED_DELTA)
        return record

    def record(self, agent: str) -> TrustRecord:
        """Return (creating if needed) the trust record for ``agent``."""
        return self._ensure_record(agent)

    def rankings(self) -> list[TrustRecord]:
        """All records ordered by trust score (desc) then agent name (asc)."""
        return sorted(
            self._records.values(),
            key=lambda r: (-r.trust_score, r.agent),
        )

    def _ensure_record(self, agent: str) -> TrustRecord:
        record = self._records.get(agent)
        if record is None:
            record = TrustRecord(agent=agent)
            self._records[agent] = record
        return record
