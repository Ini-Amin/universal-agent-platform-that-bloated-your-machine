"""knowledge layer: items, provenance, lifecycle events, pgvector search

Revision ID: b7f1c2a9d4e6
Revises: 96069bbe6bc6
Create Date: 2026-10-01 19:00:00.000000

Creates the knowledge layer (Master sections 13, 14, 15, 41, 42, 63):

* ``knowledge_items`` - curated, provenance-backed claims with lifecycle status,
  confidence, tags, optional supersession link and a ``vector(384)`` embedding.
* ``knowledge_provenance`` - one row per source backing a claim (section 15:
  "Every knowledge item must retain provenance").
* ``knowledge_events`` - the append-only lifecycle audit trail (section 15).

Kept deliberately separate from the SQLite ``memories`` store and from
filesystem artifacts (section 14).

Vector type note
----------------
The embedding column and its index opclass are rendered **schema-qualified**
(``public.vector(384)`` / ``public.vector_cosine_ops``) because the test suite
runs migrations inside scratch schemas with ``search_path`` pinned to that
schema only, while the ``vector`` extension lives in ``public``. An unqualified
type would fail to resolve there.

The ivfflat index is created with ``IF NOT EXISTS`` and is valid on an empty
table (pgvector only warns that a near-empty index is low quality). It is a
*performance* aid: exact ``<=>`` ordering is always correct, so search works
with or without it, and it becomes effective as data is backfilled.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from uap.db.extensions import ensure_pgvector_installed

revision: str = "b7f1c2a9d4e6"
down_revision: str | None = "96069bbe6bc6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_VALUES = ("proposed", "verified", "promoted", "demoted", "expired", "rejected")
_EVENT_VALUES = (
    "proposed",
    "verified",
    "promoted",
    "demoted",
    "expired",
    "rejected",
    "superseded",
)

class _Vector384(sa.types.UserDefinedType):
    """``vector(384)`` rendered schema-qualified (see module note)."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "public.vector(384)"

def _in_check(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"

def upgrade() -> None:
    # Guard only (the initial migration installs it): no-op when pgvector is
    # present, keeps this branch replayable in isolation, and never requires
    # the app role to be superuser (README Step 2C).
    ensure_pgvector_installed(op.get_bind())

    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="proposed"
        ),
        sa.Column(
            "confidence", sa.Float(), nullable=False, server_default=sa.text("0.5")
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("supersedes", sa.Uuid(), nullable=True),
        sa.Column("embedding", _Vector384(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            _in_check("status", _STATUS_VALUES),
            name=op.f("ck_knowledge_items_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_items")),
    )
    op.create_index(
        op.f("ix_knowledge_items_domain"), "knowledge_items", ["domain"], unique=False
    )
    op.create_index(
        op.f("ix_knowledge_items_status"), "knowledge_items", ["status"], unique=False
    )
    # ivfflat ANN index for cosine search. Valid on an empty table (pgvector
    # warns about low recall until data exists); IF NOT EXISTS keeps replay safe.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_items_embedding_ivfflat "
        "ON knowledge_items USING ivfflat "
        "(embedding public.vector_cosine_ops) WITH (lists = 100)"
    )

    op.create_table(
        "knowledge_provenance",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("knowledge_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("extracted_by", sa.String(length=255), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(
            ["knowledge_id"],
            ["knowledge_items.id"],
            name=op.f("fk_knowledge_provenance_knowledge_id_knowledge_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_provenance")),
    )
    op.create_index(
        op.f("ix_knowledge_provenance_knowledge_id"),
        "knowledge_provenance",
        ["knowledge_id"],
        unique=False,
    )

    op.create_table(
        "knowledge_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("knowledge_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _in_check("event", _EVENT_VALUES),
            name=op.f("ck_knowledge_events_event_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_id"],
            ["knowledge_items.id"],
            name=op.f("fk_knowledge_events_knowledge_id_knowledge_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_events")),
    )
    op.create_index(
        op.f("ix_knowledge_events_knowledge_id"),
        "knowledge_events",
        ["knowledge_id"],
        unique=False,
    )

def downgrade() -> None:
    op.drop_index(
        op.f("ix_knowledge_events_knowledge_id"), table_name="knowledge_events"
    )
    op.drop_table("knowledge_events")

    op.drop_index(
        op.f("ix_knowledge_provenance_knowledge_id"),
        table_name="knowledge_provenance",
    )
    op.drop_table("knowledge_provenance")

    op.execute("DROP INDEX IF EXISTS ix_knowledge_items_embedding_ivfflat")
    op.drop_index(op.f("ix_knowledge_items_status"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_domain"), table_name="knowledge_items")
    op.drop_table("knowledge_items")
