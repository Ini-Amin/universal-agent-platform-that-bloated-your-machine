"""decision traces, execution checkpoints, execution lease columns

Revision ID: 96069bbe6bc6
Revises: 9e4d6114bbc6
Create Date: 2026-10-01 18:00:00.000000

Additive runtime-observability foundation (Master sections 21, 26, 45, 46):

* ``decision_traces`` - durable structured operational rationale (section 26).
  Never raw chain-of-thought; ``inputs_summary`` is redacted before persistence.
* ``execution_checkpoints`` - durable per-execution snapshots (sections 21, 37,
  38) with a dense ``UNIQUE(execution_id, seq)`` counter mirroring the event log.
* ``executions.locked_by`` / ``executions.heartbeat_at`` - the worker lease used
  by the durable-runtime wave (section 39). Both nullable/additive; existing
  rows are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "96069bbe6bc6"
down_revision: str | None = "9e4d6114bbc6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    # -- executions: worker lease columns (section 39) --------------------- #
    op.add_column(
        "executions",
        sa.Column("locked_by", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )

    # -- decision_traces (section 26) -------------------------------------- #
    op.create_table(
        "decision_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.String(length=255), nullable=True),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("chosen", sa.Text(), nullable=False),
        sa.Column(
            "alternatives",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "inputs_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision_type IN ("
            "'routing', 'model_selection', 'tool_selection', 'scope_check', "
            "'policy_check', 'approval', 'retry', 'fallback', 'replan', "
            "'condition', 'synthesis')",
            name=op.f("ck_decision_traces_decision_type"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_decision_traces_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_traces")),
    )
    op.create_index(
        "ix_decision_traces_execution_id_created_at",
        "decision_traces",
        ["execution_id", "created_at"],
        unique=False,
    )

    # -- execution_checkpoints (sections 21, 37, 38) ----------------------- #
    op.create_table(
        "execution_checkpoints",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=255), nullable=True),
        sa.Column(
            "state",
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
            name=op.f("fk_execution_checkpoints_execution_id_executions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_checkpoints")),
        sa.UniqueConstraint(
            "execution_id",
            "seq",
            name=op.f("uq_execution_checkpoints_execution_id_seq"),
        ),
    )
    op.create_index(
        op.f("ix_execution_checkpoints_execution_id"),
        "execution_checkpoints",
        ["execution_id"],
        unique=False,
    )

def downgrade() -> None:
    op.drop_index(
        op.f("ix_execution_checkpoints_execution_id"),
        table_name="execution_checkpoints",
    )
    op.drop_table("execution_checkpoints")

    op.drop_index(
        "ix_decision_traces_execution_id_created_at",
        table_name="decision_traces",
    )
    op.drop_table("decision_traces")

    op.drop_column("executions", "heartbeat_at")
    op.drop_column("executions", "locked_by")
