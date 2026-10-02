"""add requested_by to executions for requester attribution

Revision ID: e7a1b4c9f2d3
Revises: d4e8b2f6a1c9
Create Date: 2026-10-03 06:30:00.000000

Adds ``requested_by`` (nullable TEXT) to the ``executions`` table.
Provides requester attribution (audit trail answering "who ran what")
without breaking existing rows. Note: this is attribution, not an authentication
boundary — callers can claim any identity.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e7a1b4c9f2d3"
down_revision: str | None = "d4e8b2f6a1c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("requested_by", sa.Text(), nullable=True))
    op.create_index(
        op.f("ix_executions_requested_by"),
        "executions",
        ["requested_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_executions_requested_by"), table_name="executions")
    op.drop_column("executions", "requested_by")
