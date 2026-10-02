"""Knowledge persistence (Master sections 13, 14, 15, 41, 42, 63).

Three tables back the knowledge layer. They are deliberately **separate** from
``memories`` (the SQLite memory store, section 16) and from filesystem
artifacts (section 19): section 14 forbids flattening Memory, Knowledge and
Artifact into one generic store.

``knowledge_items``
    One curated claim with its lifecycle status, confidence, tags, optional
    supersession link and optional pgvector embedding.
``knowledge_provenance``
    One row per source backing a claim. Section 15: "Every knowledge item must
    retain provenance". The ORM enforces a cascade so provenance never outlives
    its item.
``knowledge_events``
    The append-only lifecycle audit trail (section 15): proposed / verified /
    promoted / demoted / expired / rejected / superseded, each with an actor and
    a reason.

Vector type note
----------------
The embedding column is ``vector(384)`` (``EMBEDDING_DIM`` from
:mod:`uap.knowledge.embeddings`). :class:`Vector384` renders the type
*schema-qualified* (``public.vector(384)``) and the index opclass as
``public.vector_cosine_ops``. This is required because the test suite runs
Alembic / ``create_all`` inside scratch schemas with ``search_path`` pinned to
that schema only; the pgvector extension lives in ``public``, so an unqualified
``vector`` / ``vector_cosine_ops`` would not resolve there. Qualifying keeps the
main-``public`` case working identically.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from uap.db.base import Base

__all__ = [
    "KNOWLEDGE_EVENT_VALUES",
    "KNOWLEDGE_STATUS_VALUES",
    "KNOWLEDGE_VECTOR_DIM",
    "KnowledgeEventRow",
    "KnowledgeItemRow",
    "KnowledgeProvenanceRow",
    "Vector384",
]

#: Embedding width. Must equal :data:`uap.knowledge.embeddings.EMBEDDING_DIM`
#: (kept literal here so the low-level ``uap.db`` layer does not import the
#: higher-level ``uap.knowledge`` package - section 73 layering).
KNOWLEDGE_VECTOR_DIM = 384

#: Allowed ``knowledge_items.status`` values, mirroring
#: :class:`uap.knowledge.model.KnowledgeStatus`.
KNOWLEDGE_STATUS_VALUES: tuple[str, ...] = (
    "proposed",
    "verified",
    "promoted",
    "demoted",
    "expired",
    "rejected",
)

#: Allowed ``knowledge_events.event`` values (lifecycle audit trail, section 15).
KNOWLEDGE_EVENT_VALUES: tuple[str, ...] = (
    "proposed",
    "verified",
    "promoted",
    "demoted",
    "expired",
    "rejected",
    "superseded",
)

def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"

class Vector384(VECTOR):
    """pgvector ``vector(384)`` rendered schema-qualified (see module note)."""

    cache_ok = True

    def __init__(self) -> None:
        super().__init__(KNOWLEDGE_VECTOR_DIM)

    def get_col_spec(self, **kw: object) -> str:
        return f"public.vector({KNOWLEDGE_VECTOR_DIM})"

class KnowledgeItemRow(Base):
    """One curated, provenance-backed knowledge claim (sections 13-15)."""

    __tablename__ = "knowledge_items"
    __table_args__ = (
        CheckConstraint(
            _check_in("status", KNOWLEDGE_STATUS_VALUES),
            name="status_valid",
        ),
        # Approximate-nearest-neighbour index for cosine search. ivfflat needs
        # training data to be *effective*, but it can be created on an empty
        # table; the migration additionally guards its creation so an empty
        # table never aborts ``alembic upgrade``.
        Index(
            "ix_knowledge_items_embedding_ivfflat",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_ops={"embedding": "public.vector_cosine_ops"},
            postgresql_with={"lists": 100},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    #: The claim, one sentence.
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="proposed", index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    #: knowledge_id this row replaces (plain uuid, intentionally not a FK: a
    #: superseded item may be purged while the replacement stays).
    supersedes: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    #: pgvector embedding of ``statement``; NULL until an embedder is supplied.
    embedding: Mapped[list[float] | None] = mapped_column(Vector384(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    provenance: Mapped[list["KnowledgeProvenanceRow"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="KnowledgeProvenanceRow.id",
    )
    events: Mapped[list["KnowledgeEventRow"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="KnowledgeEventRow.id",
    )

class KnowledgeProvenanceRow(Base):
    """One source backing a knowledge claim (section 15)."""

    __tablename__ = "knowledge_provenance"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    knowledge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: "artifact" | "execution" | "external" | "human".
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: artifact_id / execution_id / URL.
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    extracted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Short quote/summary backing the claim (section 12: evidence, not truth).
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")

    item: Mapped[KnowledgeItemRow] = relationship(back_populates="provenance")

class KnowledgeEventRow(Base):
    """One append-only lifecycle event (the audit trail of section 15)."""

    __tablename__ = "knowledge_events"
    __table_args__ = (
        CheckConstraint(
            _check_in("event", KNOWLEDGE_EVENT_VALUES),
            name="event_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    knowledge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    item: Mapped[KnowledgeItemRow] = relationship(back_populates="events")
