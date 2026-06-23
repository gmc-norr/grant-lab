"""uncited_claims_jobs queue

Revision ID: 0055
Revises: 0054

The uncited-claims (LLM) background-pass queue. Mirrors evaluator_jobs (active-unique
over status IN ('queued','running')) but drops the spec/rubric staleness markers (this
check depends only on the draft) and replaces scorecard with the claims JSONB.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "uncited_claims_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claims", JSONB(), nullable=True),
        sa.Column("evaluated_draft_sha", sa.Text(), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sdk_session_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_uncited_claims_jobs_ws",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_uncited_claims_jobs_requested_by",
        ),
    )
    op.create_check_constraint(
        "ck_uncited_claims_jobs_status",
        "uncited_claims_jobs",
        "status IN ('queued','running','completed','failed')",
    )
    op.create_index(
        "ix_uncited_claims_jobs_ws_queued", "uncited_claims_jobs", ["workspace_id", "queued_at"]
    )
    op.create_index(
        "uq_uncited_claims_jobs_active",
        "uncited_claims_jobs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_uncited_claims_jobs_active", table_name="uncited_claims_jobs")
    op.drop_index("ix_uncited_claims_jobs_ws_queued", table_name="uncited_claims_jobs")
    op.drop_table("uncited_claims_jobs")
