"""Knowledge domain models (Master sections 13, 14, 15).

Knowledge is *curated, reusable information with evidence/provenance*. Master
section 14 keeps three concepts deliberately separate and this module encodes
the middle one:

``Memory``
    persisted information that shapes future behaviour
    (:class:`uap.contracts.Memory`).
``Knowledge``
    curated/reusable information **with evidence and provenance** - this file.
``Artifact``
    a persistent output/file/result (:class:`uap.contracts.Artifact`).

Knowledge is therefore neither a ``Memory`` nor an ``Artifact``: it carries its
own lifecycle (section 15) and *every* item must retain provenance. A knowledge
item is never invented - it is extracted from an artifact, an execution, an
external source, or a human, and the caller supplies the one-sentence claim
(see :meth:`uap.knowledge.lifecycle.KnowledgeLifecycle.propose_from_artifact`).

Confidence is explicitly **not** truth (section 12): it is a soft score, while
``status`` plus ``verification`` are the auditable signals.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uap.contracts import UTCDateTime, VerificationResult, utc_now

__all__ = ["SOURCE_KINDS", "KnowledgeItem", "KnowledgeStatus", "Provenance"]

_MODEL_CONFIG = ConfigDict(extra="forbid")

#: Allowed provenance source kinds (Master section 13: web/API/MCP/local files/
#: workspace artifacts/databases/external systems collapse onto these four
#: origins - an artifact, an execution, an external system, or a human).
SOURCE_KINDS: tuple[str, ...] = ("artifact", "execution", "external", "human")

class KnowledgeStatus(StrEnum):
    """Lifecycle state of one knowledge item (Master section 15).

    ``PROPOSED`` -> ``VERIFIED`` -> ``PROMOTED``; a ``PROMOTED`` item may later
    be ``DEMOTED``, and any live item may ``EXPIRE`` or be ``REJECTED``.
    """

    PROPOSED = "proposed"
    VERIFIED = "verified"
    PROMOTED = "promoted"
    DEMOTED = "demoted"
    EXPIRED = "expired"
    REJECTED = "rejected"

class Provenance(BaseModel):
    """Where one knowledge claim came from (Master sections 13, 15).

    Provenance is mandatory: a :class:`KnowledgeItem` without at least one
    provenance entry cannot be constructed. ``evidence`` is a short quote or
    summary that backs the claim - it is *evidence*, not a verdict.
    """

    model_config = _MODEL_CONFIG

    source_kind: str
    #: artifact_id / execution_id / URL, depending on ``source_kind``.
    source_ref: str
    #: The agent / workflow that extracted the claim.
    extracted_by: str
    extracted_at: UTCDateTime
    #: Short quote/summary backing the claim (Master section 12: evidence).
    evidence: str = ""

    @field_validator("source_kind")
    @classmethod
    def _check_source_kind(cls, value: str) -> str:
        if value not in SOURCE_KINDS:
            raise ValueError(
                f"source_kind must be one of {SOURCE_KINDS!r}, got {value!r}"
            )
        return value

class KnowledgeItem(BaseModel):
    """One curated, reusable, provenance-backed claim (Master sections 13-15).

    ``statement`` is a single-sentence claim. ``provenance`` must contain at
    least one entry - this is the structural guarantee behind section 15's
    "Every knowledge item must retain provenance".
    """

    model_config = _MODEL_CONFIG

    knowledge_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    #: The claim itself, one sentence.
    statement: str
    domain: str
    status: KnowledgeStatus = KnowledgeStatus.PROPOSED
    #: >= 1 entry; enforced by the validator below.
    provenance: list[Provenance]
    #: Soft score in [0, 1]. Confidence is NOT truth (section 12).
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Explicit criteria-based verdict from the verifier (section 17).
    verification: VerificationResult | None = None
    tags: list[str] = Field(default_factory=list)
    #: knowledge_id this item replaces, when it supersedes another claim.
    supersedes: str | None = None
    created_at: UTCDateTime = Field(default_factory=utc_now)
    updated_at: UTCDateTime = Field(default_factory=utc_now)
    expires_at: UTCDateTime | None = None

    @field_validator("provenance")
    @classmethod
    def _require_provenance(cls, value: list[Provenance]) -> list[Provenance]:
        if not value:
            raise ValueError(
                "knowledge requires at least one provenance entry "
                "(Master section 15)"
            )
        return value
