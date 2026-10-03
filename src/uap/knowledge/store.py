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

def _normalize_statement(statement: str) -> str:
    """Casefold + collapse whitespace so "Python ..." == "python ...".

    The duplicate-detection key. Two claims that differ only by case or
    surrounding/embedded whitespace are the same claim; the caller's exact
    spelling is what gets stored.
    """

    return " ".join(str(statement or "").split()).casefold()

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

    def delete(self, knowledge_id: str) -> bool:
        """Delete one item and its provenance/events (cascade). ``False`` if unknown.

        The item's child rows are removed by the ``ON DELETE CASCADE`` foreign
        keys (section 15: provenance never outlives its item). Returns whether a
        row was actually removed so the caller can answer 404 honestly.
        """

        try:
            key = uuid.UUID(knowledge_id)
        except (ValueError, AttributeError, TypeError):
            return False
        result = self._session.execute(
            delete(KnowledgeItemRow).where(KnowledgeItemRow.id == key)
        )
        self._session.flush()
        return bool(result.rowcount)

    def dedupe_by_statement(self, *, keep: str = "oldest") -> int:
        """Collapse items that repeat the same claim in the same domain.

        Two items are duplicates when their ``statement`` matches
        case-insensitively (surrounding whitespace ignored) *and* they share a
        ``domain``. One item per group survives -- the ``oldest`` (default) or
        ``newest`` by ``created_at`` -- and the rest are deleted, their
        provenance/event rows cascading with them. Returns the number removed.

        This is the cleanup half of the duplicate-knowledge fix; the prevention
        half is the caller refusing to insert a claim that already exists (see
        :meth:`find_by_statement`).
        """

        rows = list(
            self._session.execute(
                select(
                    KnowledgeItemRow.id,
                    KnowledgeItemRow.statement,
                    KnowledgeItemRow.domain,
                    KnowledgeItemRow.created_at,
                ).order_by(
                    KnowledgeItemRow.created_at.asc(), KnowledgeItemRow.id.asc()
                )
            ).all()
        )
        winner: dict[tuple[str, str], uuid.UUID] = {}
        for row_id, statement, domain, _created in rows:
            key = (str(domain), _normalize_statement(statement))
            if key not in winner:
                winner[key] = row_id
            elif keep == "newest":
                winner[key] = row_id
        # Everything not selected as the winner is a duplicate.
        losers = [row_id for row_id, statement, domain, _c in rows
                  if winner[(str(domain), _normalize_statement(statement))] != row_id]
        if not losers:
            return 0
        self._session.execute(
            delete(KnowledgeItemRow).where(KnowledgeItemRow.id.in_(losers))
        )
        self._session.flush()
        return len(losers)

    # -- read --------------------------------------------------------------- #

    def get(self, knowledge_id: str) -> KnowledgeItem | None:
        """Return the item, or ``None`` when unknown."""

        row = self._session.get(KnowledgeItemRow, uuid.UUID(knowledge_id))
        return None if row is None else self._row_to_item(row)

    def find_by_statement(
        self, statement: str, domain: str | None = None
    ) -> KnowledgeItem | None:
        """Return an existing item with the same claim (and domain), or ``None``.

        The comparison is case-insensitive and whitespace-normalised, matching
        :meth:`dedupe_by_statement`. Callers use this to refuse a duplicate
        insert instead of creating one, which is the source of the duplication
        the cleanup removes.
        """

        normalized = _normalize_statement(statement)
        if not normalized:
            return None
        stmt = select(KnowledgeItemRow)
        if domain is not None:
            stmt = stmt.where(KnowledgeItemRow.domain == domain)
        stmt = stmt.order_by(KnowledgeItemRow.created_at.asc())
        for row in self._session.execute(stmt).scalars():
            if _normalize_statement(row.statement) == normalized:
                return self._row_to_item(row)
        return None

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
