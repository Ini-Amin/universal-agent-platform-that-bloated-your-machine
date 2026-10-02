"""initial persistence foundation

Revision ID: 9e4d6114bbc6
Revises:
Create Date: 2026-10-01 17:05:54.649031

Creates the Phase 0 persistence foundation (Master sections 41, 42, 63, 67):

* ``workflow_definitions`` / ``workflow_versions``
* ``agent_definitions`` / ``agent_versions``
* ``tool_definitions`` / ``tool_versions``
* ``skill_definitions`` / ``skill_versions``
* ``executions``
* ``execution_events``

and enables the ``vector`` extension for pgvector (Master section 42).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "9e4d6114bbc6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector (Master sections 41, 42, 64). Idempotent; requires a role that
    # may install extensions (owner/superuser), which is the local-first setup.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "agent_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_definitions")),
        sa.UniqueConstraint("name", name=op.f("uq_agent_definitions_name")),
    )
    op.create_table(
        "skill_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_definitions")),
        sa.UniqueConstraint("name", name=op.f("uq_skill_definitions_name")),
    )
    op.create_table(
        "tool_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_definitions")),
        sa.UniqueConstraint("name", name=op.f("uq_tool_definitions_name")),
    )
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_definitions")),
        sa.UniqueConstraint("name", name=op.f("uq_workflow_definitions_name")),
    )

    _version_status_enum = sa.Enum(
        "draft",
        "active",
        "deprecated",
        name="version_status_enum",
        native_enum=False,
        create_constraint=True,
        length=16,
    )
    for table_name, fk_table in (
        ("agent_versions", "agent_definitions"),
        ("skill_versions", "skill_definitions"),
        ("tool_versions", "tool_definitions"),
        ("workflow_versions", "workflow_definitions"),
    ):
        op.create_table(
            table_name,
            sa.Column("definition_id", sa.Uuid(), nullable=False),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("status", _version_status_enum, nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(
                ["definition_id"],
                [f"{fk_table}.id"],
                name=op.f(f"fk_{table_name}_definition_id_{fk_table}"),
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table_name}")),
            sa.UniqueConstraint(
                "definition_id",
                "version",
                name=op.f(f"uq_{table_name}_definition_id_version"),
            ),
        )

    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "paused",
                "awaiting_approval",
                "completed",
                "failed",
                "cancelled",
                name="execution_status_enum",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
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
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("resume_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_executions_workflow_version_id_workflow_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_executions")),
    )
    op.create_index(
        op.f("ix_executions_correlation_id"),
        "executions",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(op.f("ix_executions_status"), "executions", ["status"], unique=False)
    op.create_index(
        op.f("ix_executions_workflow_version_id"),
        "executions",
        ["workflow_version_id"],
        unique=False,
    )

    op.create_table(
        "execution_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("node", sa.String(length=255), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_execution_events_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_events")),
        sa.UniqueConstraint(
            "execution_id", "seq", name=op.f("uq_execution_events_execution_id_seq")
        ),
    )
    op.create_index(
        op.f("ix_execution_events_execution_id"),
        "execution_events",
        ["execution_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_execution_events_execution_id"), table_name="execution_events"
    )
    op.drop_table("execution_events")
    op.drop_index(op.f("ix_executions_workflow_version_id"), table_name="executions")
    op.drop_index(op.f("ix_executions_status"), table_name="executions")
    op.drop_index(op.f("ix_executions_correlation_id"), table_name="executions")
    op.drop_table("executions")
    op.drop_table("workflow_versions")
    op.drop_table("tool_versions")
    op.drop_table("skill_versions")
    op.drop_table("agent_versions")
    op.drop_table("workflow_definitions")
    op.drop_table("tool_definitions")
    op.drop_table("skill_definitions")
    op.drop_table("agent_definitions")
