"""workspaces table (Master section 3)

Revision ID: c3a7f1e9d2b4
Revises: b7f1c2a9d4e6
Create Date: 2026-10-03 06:30:00.000000

The Workspace layer was specified from the start (Master section 3) and the
value object, detection and store all shipped -- but the ORM table never did, so
``POST /api/workspaces`` returned 503 ("no Workspace ORM model") and the
template catalog had nowhere to create a workspace. This migration lands the
missing table.

Columns mirror :class:`~uap.workspace.model.Workspace` exactly:

* ``id`` (string, chosen by the creator), ``name``, ``description``,
  ``root_path``, ``status`` (active/archived/deleted);
* ``created_at`` / ``updated_at`` timestamps;
* ``settings`` and ``default_workflow_refs`` as JSONB so a workspace can pin
  exact Library refs without a schema change.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c3a7f1e9d2b4"
down_revision: str | None = "b7f1c2a9d4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("root_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
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
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "default_workflow_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    op.create_index(op.f("ix_workspaces_name"), "workspaces", ["name"], unique=False)
    op.create_index(op.f("ix_workspaces_status"), "workspaces", ["status"], unique=False)

def downgrade() -> None:
    op.drop_index(op.f("ix_workspaces_status"), table_name="workspaces")
    op.drop_index(op.f("ix_workspaces_name"), table_name="workspaces")
    op.drop_table("workspaces")
