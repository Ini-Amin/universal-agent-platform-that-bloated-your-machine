"""PostgreSQL/pgvector-backed knowledge store (Master sections 13, 14, 15).

:class:`KnowledgeStore` takes an injected :class:`~sqlalchemy.orm.Session` and
owns every query against the three knowledge tables. It never commits -
transaction boundaries belong to the caller (mirroring the repositories in
:mod:`uap.db.repositories`, Master section 73: no hidden global state).

Search
------
:meth:`KnowledgeStore.search` returns ``(item, cosine_distance)`` pairs, nearest
first, using pgvector's ``<=>`` cosine-distance operator. Rows whose
``embedding`` is ``NULL`` (added without an embedder) are **not** silently
dropped: they are matched by a case-insensitive ``statement ILIKE`` fallback and
appended with :data:`FALLBACK_COSINE_DISTANCE`. This keeps a partially-embedded
knowledge base searchable while the embeddings are backfilled.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from uap.contracts import utc_now
from uap.db.models.knowledge import (
    KnowledgeEventRow,
    KnowledgeItemRow,
    KnowledgeProvenanceRow,
)
from uap.knowledge.embeddings import Embedder
from uap.knowledge.model import KnowledgeItem, KnowledgeStatus, Provenance

__all__ = ["FALLBACK_COSINE_DISTANCE", "KnowledgeStore"]

#: Distance assigned to text-fallback hits (rows with a NULL embedding). Cosine
#: distance is bounded in ``[0, 2]``; ``1.0`` is the neutral sentinel for "no
#: vector was available to measure against".
FALLBACK_COSINE_DISTANCE = 1.0

class KnowledgeStore:
    """Typed persistence for knowledge items, provenance and lifecycle events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- write -------------------------------------------------------------- #

    def add(
        self,
        item: KnowledgeItem,
        embedder: Embedder | None = None,
        *,
        actor: str | None = None,
    ) -> KnowledgeItem:
        """Insert ``item`` plus its provenance rows and a ``proposed`` event.

        When ``embedder`` is supplied the ``statement`` is embedded into the
        ``vector(384)`` column; otherwise the column stays ``NULL`` and the row
        is still discoverable through the ILIKE fallback in :meth:`search`.
        ``actor`` defaults to the first provenance's ``extracted_by`` (the party
        that extracted the claim), so the audit trail is never anonymous.
        """

        row = KnowledgeItemRow(
            id=uuid.UUID(item.knowledge_id),
            statement=item.statement,
            domain=item.domain,
            status=item.status.value,
            confidence=item.confidence,
            tags=list(item.tags),
            supersedes=uuid.UUID(item.supersedes) if item.supersedes else None,
            embedding=embedder.embed(item.statement) if embedder is not None else None,
            created_at=item.created_at,
            updated_at=item.updated_at,
            expires_at=item.expires_at,
        )
        for source in item.provenance:
            row.provenance.append(
                KnowledgeProvenanceRow(
                    source_kind=source.source_kind,
                    source_ref=source.source_ref,
                    extracted_by=source.extracted_by,
                    extracted_at=source.extracted_at,
                    evidence=source.evidence,
                )
            )
        self._session.add(row)
        self._session.flush()

        self._append_event(
            row.id,
            "proposed",
            actor=actor or item.provenance[0].extracted_by,
            reason="",
        )
        return self._row_to_item(row)

    def update_status(
        self,
        knowledge_id: str,
        status: KnowledgeStatus,
        *,
        actor: str,
        reason: str = "",
    ) -> KnowledgeItem:
        """Transition an item's lifecycle status and record the event."""

        row = self._require_row(knowledge_id)
        row.status = status.value
        row.updated_at = utc_now()
        self._session.flush()
        self._append_event(row.id, status.value, actor=actor, reason=reason)
        return self._row_to_item(row)

    def supersede(
        self, old_id: str, new_item: KnowledgeItem, *, actor: str
    ) -> KnowledgeItem:
        """Replace ``old_id`` with ``new_item`` (Master section 15).

        The new item is inserted carrying ``supersedes = old_id``. The old item
        moves to :attr:`KnowledgeStatus.DEMOTED` and an ``superseded`` event is
        recorded on it (the lifecycle vocabulary of section 15 has no dedicated
        ``superseded`` *status*, so ``DEMOTED`` is the chosen terminal state and
        the ``superseded`` event preserves the why/when/who).
        """

        old = self._require_row(old_id)
        linked = new_item.model_copy(update={"supersedes": str(old.id)})
        created = self.add(linked, actor=actor)

        old.status = KnowledgeStatus.DEMOTED.value
        old.updated_at = utc_now()
        self._session.flush()
        self._append_event(
            old.id,
            "superseded",
            actor=actor,
            reason=f"superseded by {created.knowledge_id}",
        )
        return created

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete every item whose ``expires_at`` is before ``now``.

        Returns the number of rows removed. Child provenance/event rows are
        removed by the ``ON DELETE CASCADE`` foreign keys.
        """

        moment = now or utc_now()
        result = self._session.execute(
            delete(KnowledgeItemRow).where(
                KnowledgeItemRow.expires_at.is_not(None),
                KnowledgeItemRow.expires_at < moment,
            )
        )
        self._session.flush()
        return int(result.rowcount or 0)

    # -- read --------------------------------------------------------------- #

    def get(self, knowledge_id: str) -> KnowledgeItem | None:
        """Return the item, or ``None`` when unknown."""

        row = self._session.get(KnowledgeItemRow, uuid.UUID(knowledge_id))
        return None if row is None else self._row_to_item(row)

    def search(
        self,
        query: str,
        *,
        embedder: Embedder,
        domain: str | None = None,
        statuses: list[KnowledgeStatus] | None = None,
        limit: int = 10,
    ) -> list[tuple[KnowledgeItem, float]]:
        """Return ``(item, cosine_distance)`` pairs, nearest first.

        Vector rows are ranked by ``embedding <=> :query_vector`` (cosine
        distance). Rows with a ``NULL`` embedding are matched by a
        case-insensitive ``statement ILIKE`` and appended afterwards.
        """

        filters = []
        if domain is not None:
            filters.append(KnowledgeItemRow.domain == domain)
        if statuses is not None:
            filters.append(
                KnowledgeItemRow.status.in_([status.value for status in statuses])
            )

        query_vector = embedder.embed(query)
        distance = KnowledgeItemRow.embedding.cosine_distance(query_vector).label(
            "cosine_distance"
        )
        vector_stmt = (
            select(KnowledgeItemRow, distance)
            .where(KnowledgeItemRow.embedding.is_not(None), *filters)
            .order_by(distance)
            .limit(limit)
        )
        results: list[tuple[KnowledgeItem, float]] = [
            (self._row_to_item(row), float(value))
            for row, value in self._session.execute(vector_stmt).all()
        ]

        if len(results) < limit:
            text_stmt = (
                select(KnowledgeItemRow)
                .where(
                    KnowledgeItemRow.embedding.is_(None),
                    KnowledgeItemRow.statement.ilike(f"%{query}%"),
                    *filters,
                )
                .order_by(KnowledgeItemRow.created_at.desc())
                .limit(limit - len(results))
            )
            for row in self._session.execute(text_stmt).scalars().all():
                results.append((self._row_to_item(row), FALLBACK_COSINE_DISTANCE))

        return results[:limit]

    def list_by_status(
        self, status: KnowledgeStatus, limit: int = 100
    ) -> list[KnowledgeItem]:
        """List items in ``status``, newest first."""

        stmt = (
            select(KnowledgeItemRow)
            .where(KnowledgeItemRow.status == status.value)
            .order_by(KnowledgeItemRow.created_at.desc(), KnowledgeItemRow.id.desc())
            .limit(limit)
        )
        return [self._row_to_item(row) for row in self._session.execute(stmt).scalars()]

    def history(self, knowledge_id: str) -> list[dict]:
        """Return the ordered lifecycle events for an item (section 15).

        Each entry is ``{"event", "actor", "reason", "created_at"}`` with
        ``created_at`` rendered as an ISO-8601 string.
        """

        stmt = (
            select(KnowledgeEventRow)
            .where(KnowledgeEventRow.knowledge_id == uuid.UUID(knowledge_id))
            .order_by(KnowledgeEventRow.id.asc())
        )
        return [
            {
                "event": event.event,
                "actor": event.actor,
                "reason": event.reason,
                "created_at": event.created_at.isoformat(),
            }
            for event in self._session.execute(stmt).scalars()
        ]

    # -- internal ----------------------------------------------------------- #

    def _require_row(self, knowledge_id: str) -> KnowledgeItemRow:
        row = self._session.get(KnowledgeItemRow, uuid.UUID(knowledge_id))
        if row is None:
            raise LookupError(f"knowledge item {knowledge_id} not found")
        return row

    def _append_event(
        self, knowledge_id: uuid.UUID, event: str, *, actor: str, reason: str
    ) -> KnowledgeEventRow:
        row = KnowledgeEventRow(
            knowledge_id=knowledge_id,
            event=event,
            actor=actor,
            reason=reason,
            created_at=utc_now(),
        )
        self._session.add(row)
        self._session.flush()
        return row

    @staticmethod
    def _row_to_item(row: KnowledgeItemRow) -> KnowledgeItem:
        return KnowledgeItem(
            knowledge_id=str(row.id),
            statement=row.statement,
            domain=row.domain,
            status=KnowledgeStatus(row.status),
            provenance=[
                Provenance(
                    source_kind=source.source_kind,
                    source_ref=source.source_ref,
                    extracted_by=source.extracted_by,
                    extracted_at=source.extracted_at,
                    evidence=source.evidence,
                )
                for source in row.provenance
            ],
            confidence=row.confidence,
            tags=list(row.tags or []),
            supersedes=str(row.supersedes) if row.supersedes else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
            expires_at=row.expires_at,
        )
