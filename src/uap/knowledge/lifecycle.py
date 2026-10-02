"""Policy-gated knowledge lifecycle (Master sections 15, 48, 69).

Agents may *propose* knowledge; policy decides whether it can be promoted.
This module encodes section 15's rules as an explicit, inspectable policy
object plus a small state machine over :class:`~uap.knowledge.store.KnowledgeStore`:

```text
propose ──▶ PROPOSED ──verify(pass)──▶ VERIFIED ──promote(policy ok)──▶ PROMOTED
                │                          │                               │
                │ verify(fail): stays       │ expire_overdue                │ demote
                │ PROPOSED + note           ▼                               ▼
                │                        EXPIRED                        DEMOTED
                └── supersede ──▶ DEMOTED
```

Nothing is persisted automatically without going through :meth:`propose` and,
for promotion, a satisfied :class:`PromotionPolicy` (section 69, rule 15:
"Promote persistent changes only through versioned lifecycle").

Verification persistence note
-----------------------------
``knowledge_items`` has no verification column (the table shape is fixed by the
assignment), so a *passing* verification is made durable through the
``VERIFIED`` **status** rather than the ``VerificationResult`` object itself.
:meth:`KnowledgeLifecycle.promote` therefore treats ``status == VERIFIED`` (or a
still-attached passing ``verification``) as "verification passed".
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from uap.contracts import Artifact, VerificationResult, utc_now
from uap.knowledge.embeddings import Embedder
from uap.knowledge.model import KnowledgeItem, KnowledgeStatus, Provenance
from uap.knowledge.store import KnowledgeStore

__all__ = ["KnowledgeLifecycle", "PolicyDeniedError", "PromotionPolicy"]

class PromotionPolicy(BaseModel):
    """Deterministic gate for ``PROPOSED``/``VERIFIED`` -> ``PROMOTED``.

    A promotion is denied (with the full list of reasons) when any active rule
    fails. ``allowed_domains=None`` means "all domains allowed".
    """

    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    min_provenance: int = Field(default=1, ge=0)
    require_verification_pass: bool = True
    allowed_domains: list[str] | None = None

class PolicyDeniedError(Exception):
    """Raised when a promotion fails one or more policy rules."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("promotion denied: " + "; ".join(self.reasons))

#: Statuses that count as evidence of a passing verification (see module note).
_VERIFIED_STATUSES = frozenset({KnowledgeStatus.VERIFIED, KnowledgeStatus.PROMOTED})

class KnowledgeLifecycle:
    """Policy-gated transitions over a :class:`KnowledgeStore`."""

    def __init__(
        self,
        store: KnowledgeStore,
        policy: PromotionPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy if policy is not None else PromotionPolicy()

    # -- propose / verify --------------------------------------------------- #

    def propose(
        self,
        item: KnowledgeItem,
        *,
        actor: str,
        embedder: Embedder | None = None,
    ) -> KnowledgeItem:
        """Persist a candidate as ``PROPOSED`` (writes the ``proposed`` event)."""

        return self.store.add(item, embedder, actor=actor)

    def verify(
        self,
        knowledge_id: str,
        verification: VerificationResult,
        *,
        actor: str,
    ) -> KnowledgeItem:
        """Record a verification verdict.

        On success the item moves to ``VERIFIED``. On failure it stays
        ``PROPOSED`` and the failure is recorded as an event whose ``reason``
        states the verdict (the lifecycle vocabulary of section 15 has no
        dedicated failure event, so the event mirrors the *resulting* status).
        """

        if verification.passed:
            updated = self.store.update_status(
                knowledge_id,
                KnowledgeStatus.VERIFIED,
                actor=actor,
                reason=verification.notes or f"verified by {verification.verifier}",
            )
        else:
            updated = self.store.update_status(
                knowledge_id,
                KnowledgeStatus.PROPOSED,
                actor=actor,
                reason=(
                    f"verification failed by {verification.verifier}: "
                    f"{verification.notes or 'no notes'}"
                ),
            )
        return updated.model_copy(update={"verification": verification})

    # -- promote / demote --------------------------------------------------- #

    def promote(self, knowledge_id: str, *, actor: str) -> KnowledgeItem:
        """Promote to ``PROMOTED`` when the policy is satisfied.

        Raises :class:`PolicyDeniedError` carrying every unmet reason otherwise.
        """

        item = self.store.get(knowledge_id)
        if item is None:
            raise LookupError(f"knowledge item {knowledge_id} not found")

        reasons = self._policy_reasons(item)
        if reasons:
            raise PolicyDeniedError(reasons)

        return self.store.update_status(
            knowledge_id,
            KnowledgeStatus.PROMOTED,
            actor=actor,
            reason="promotion policy satisfied",
        )

    def demote(self, knowledge_id: str, *, actor: str, reason: str) -> KnowledgeItem:
        """Move an item to ``DEMOTED`` (manual, section 15)."""

        return self.store.update_status(
            knowledge_id, KnowledgeStatus.DEMOTED, actor=actor, reason=reason
        )

    # -- expiry ------------------------------------------------------------- #

    def expire_overdue(self, now: datetime | None = None) -> int:
        """Transition every overdue live item to ``EXPIRED``.

        Returns the number of items expired. Only items that are still "live"
        (``PROPOSED`` / ``VERIFIED`` / ``PROMOTED`` / ``DEMOTED``) are touched;
        already-terminal items are left alone.
        """

        moment = now or utc_now()
        live = (
            KnowledgeStatus.PROPOSED,
            KnowledgeStatus.VERIFIED,
            KnowledgeStatus.PROMOTED,
            KnowledgeStatus.DEMOTED,
        )
        expired = 0
        for status in live:
            for item in self.store.list_by_status(status, limit=10_000):
                if item.expires_at is not None and item.expires_at < moment:
                    self.store.update_status(
                        item.knowledge_id,
                        KnowledgeStatus.EXPIRED,
                        actor="lifecycle",
                        reason=f"expired at {moment.isoformat()}",
                    )
                    expired += 1
        return expired

    # -- artifact-sourced proposal (section 69: never hallucinate) ---------- #

    def propose_from_artifact(
        self,
        artifact: Artifact,
        *,
        statement: str,
        domain: str,
        actor: str,
        embedder: Embedder | None = None,
    ) -> KnowledgeItem:
        """Propose a claim extracted from ``artifact``.

        The ``statement`` is supplied by the caller: this method never invents
        content (section 69, rule 7: preserve provenance; a knowledge item is
        *extracted with provenance, not hallucinated*). Provenance is derived
        from the artifact's own fields.

        When ``embedder`` is supplied the statement is also written to the
        ``vector(384)`` column, enabling vector search instead of the ILIKE
        fallback (the fallback still covers rows with a NULL embedding).
        """

        provenance = Provenance(
            source_kind="artifact",
            source_ref=artifact.artifact_id,
            extracted_by=actor,
            extracted_at=utc_now(),
            evidence=artifact.content_ref or artifact.uri or artifact.type,
        )
        item = KnowledgeItem(
            statement=statement,
            domain=domain,
            provenance=[provenance],
        )
        return self.propose(item, actor=actor, embedder=embedder)

    # -- internal ----------------------------------------------------------- #

    def _policy_reasons(self, item: KnowledgeItem) -> list[str]:
        policy = self.policy
        reasons: list[str] = []

        if item.confidence < policy.min_confidence:
            reasons.append(
                f"confidence {item.confidence:.3f} < min_confidence "
                f"{policy.min_confidence:.3f}"
            )
        if len(item.provenance) < policy.min_provenance:
            reasons.append(
                f"provenance count {len(item.provenance)} < min_provenance "
                f"{policy.min_provenance}"
            )
        if policy.require_verification_pass and not self._verification_passed(item):
            reasons.append("verification has not passed")
        if policy.allowed_domains is not None and item.domain not in policy.allowed_domains:
            reasons.append(f"domain {item.domain!r} not in allowed_domains")

        return reasons

    @staticmethod
    def _verification_passed(item: KnowledgeItem) -> bool:
        if item.verification is not None and item.verification.passed:
            return True
        return item.status in _VERIFIED_STATUSES
