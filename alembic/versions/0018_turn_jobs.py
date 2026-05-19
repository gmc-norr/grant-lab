"""turn_jobs

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "turn_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_turn_id"],
            ["chat_turns.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_turn_jobs_status",
        ),
    )
    op.create_index(
        "ix_turn_jobs_ws_queued",
        "turn_jobs",
        ["workspace_id", "queued_at"],
    )
    # Single-running-turn-per-workspace enforcement.
    op.create_index(
        "uq_turn_jobs_running",
        "turn_jobs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_turn_jobs_running", table_name="turn_jobs")
    op.drop_index("ix_turn_jobs_ws_queued", table_name="turn_jobs")
    op.drop_table("turn_jobs")
