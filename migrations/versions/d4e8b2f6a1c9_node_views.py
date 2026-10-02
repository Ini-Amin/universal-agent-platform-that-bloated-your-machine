"""node views: the work a node published for the canvas

Revision ID: d4e8b2f6a1c9
Revises: c3a7f1e9d2b4
Create Date: 2026-10-03 06:10:00.000000

Creates ``node_views`` (canvas-as-stage): one row per ``(execution_id,
node_id)`` holding the view a node published as the visible product of its work
(video, markdown, code, image, iframe, whiteboard). The canvas reads this table
so a node's work survives a page reload.

A dedicated table (rather than reusing event payloads) keeps the append-only
``execution_events`` status-replay stream small and the read API a single
indexed lookup; ``UNIQUE(execution_id, node_id)`` makes a republish an
idempotent upsert. ``execution_id`` cascades with its execution.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d4e8b2f6a1c9"
down_revision: str | None = "c3a7f1e9d2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "node_views",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column(
            "view",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_node_views_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_views")),
        sa.UniqueConstraint(
            "execution_id",
            "node_id",
            name=op.f("uq_node_views_execution_id_node_id"),
        ),
    )
    op.create_index(
        op.f("ix_node_views_execution_id"),
        "node_views",
        ["execution_id"],
        unique=False,
    )

def downgrade() -> None:
    op.drop_index(op.f("ix_node_views_execution_id"), table_name="node_views")
    op.drop_table("node_views")
