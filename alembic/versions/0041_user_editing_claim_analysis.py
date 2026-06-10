"""Three tables for the claim-analysis feature: claim_analysis_jobs,
claim_classifications, user_claim_annotations.

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-10

See docs/superpowers/plans/2026-06-10-user-editing-claim-analysis.md Task 4.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # claim_analysis_jobs
    # ------------------------------------------------------------------
    op.create_table(
        "claim_analysis_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("section_index", sa.Integer(), nullable=False),
        sa.Column("section_heading", sa.Text(), nullable=False, server_default=""),
        sa.Column("section_sha", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','superseded')",
            name="ck_claim_analysis_jobs_status",
        ),
    )
    # Partial unique index for enqueue deduplication.
    op.create_index(
        "uq_claim_analysis_jobs_ws_sha_queued",
        "claim_analysis_jobs",
        ["workspace_id", "section_sha"],
        unique=True,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_claim_analysis_jobs_status_queued_at",
        "claim_analysis_jobs",
        ["status", "queued_at"],
    )

    # ------------------------------------------------------------------
    # claim_classifications
    # ------------------------------------------------------------------
    op.create_table(
        "claim_classifications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_sha", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(16), nullable=False),
        sa.Column("is_claim", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=True),
        sa.Column("evidence_quote", sa.Text(), nullable=True),
        sa.Column("verdict_json", postgresql.JSONB(), nullable=True),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reference_id"],
            ["references.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "outcome IN ('not_claim','supported','no_match')",
            name="ck_claim_classifications_outcome",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "claim_sha",
            "prompt_version",
            name="uq_claim_classifications_ws_sha_pv",
        ),
    )

    # ------------------------------------------------------------------
    # user_claim_annotations
    # ------------------------------------------------------------------
    op.create_table(
        "user_claim_annotations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("section_sha", sa.String(64), nullable=False),
        sa.Column("section_occurrence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("section_heading", sa.Text(), nullable=False, server_default=""),
        sa.Column("sentence_ordinal", sa.Integer(), nullable=False),
        sa.Column("char_offset", sa.Integer(), nullable=False),
        sa.Column("claim_sha", sa.String(64), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("evidence_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=True),
        sa.Column("evidence_quote", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("dismissed_reason", sa.Text(), nullable=True),
        sa.Column("resolved_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reference_id"],
            ["references.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "kind IN ('uncited_supported','uncited_no_match','cited_unsupported')",
            name="ck_user_claim_annotations_kind",
        ),
        sa.CheckConstraint(
            "status IN ('open','inserted','dismissed','stale')",
            name="ck_user_claim_annotations_status",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "section_sha",
            "section_occurrence",
            "sentence_ordinal",
            "kind",
            "evidence_id",
            name="uq_user_claim_annotations_occurrence",
        ),
    )
    op.create_index(
        "ix_user_claim_annotations_ws_status",
        "user_claim_annotations",
        ["workspace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_claim_annotations_ws_status",
        table_name="user_claim_annotations",
    )
    op.drop_table("user_claim_annotations")
    op.drop_table("claim_classifications")
    op.drop_index(
        "ix_claim_analysis_jobs_status_queued_at",
        table_name="claim_analysis_jobs",
    )
    op.drop_index(
        "uq_claim_analysis_jobs_ws_sha_queued",
        table_name="claim_analysis_jobs",
    )
    op.drop_table("claim_analysis_jobs")
