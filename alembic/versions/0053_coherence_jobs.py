"""coherence_jobs queue

Revision ID: 0053
Revises: 0052

The coherence-keeper's background-pass queue. Mirrors turn_jobs, but the
active-unique index covers status IN ('queued','running') (turn_jobs is
running-only): at most one coherence pass per workspace may be pending+running.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coherence_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sdk_session_id", sa.Text(), nullable=True),
        sa.Column("n_proposals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_coherence_jobs_ws"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_coherence_jobs_requested_by",
        ),
    )
    op.create_check_constraint(
        "ck_coherence_jobs_status",
        "coherence_jobs",
        "status IN ('queued','running','completed','failed')",
    )
    op.create_index("ix_coherence_jobs_ws_queued", "coherence_jobs", ["workspace_id", "queued_at"])
    op.create_index(
        "uq_coherence_jobs_active",
        "coherence_jobs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_coherence_jobs_active", table_name="coherence_jobs")
    op.drop_index("ix_coherence_jobs_ws_queued", table_name="coherence_jobs")
    op.drop_table("coherence_jobs")
