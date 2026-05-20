"""review_runs + review_run_items

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draft_sha", sa.Text(), nullable=False),
        sa.Column("fit_check_sha", sa.Text(), nullable=True),
        sa.Column("rubric_review_sha", sa.Text(), nullable=True),
        sa.Column(
            "waived_from_review_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_dollars", sa.Numeric(6, 3), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["waived_from_review_run_id"],
            ["review_runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','cancelled','partial')",
            name="ck_review_runs_status",
        ),
    )
    op.create_index(
        "ix_review_runs_ws_triggered",
        "review_runs",
        ["workspace_id", sa.text("triggered_at DESC")],
    )
    # Active-review lock: one queued or running run per workspace.
    op.create_index(
        "uq_review_runs_active",
        "review_runs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )

    op.create_table(
        "review_run_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_dollars", sa.Numeric(6, 4), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "raw_response_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["review_run_id"],
            ["review_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_run_id",
            "kind",
            name="uq_review_run_items_run_kind",
        ),
        sa.CheckConstraint(
            "kind IN ('fit_check',"
            "'persona_domain_skeptic',"
            "'persona_methodology_critic',"
            "'persona_translation_skeptic',"
            "'persona_devils_advocate',"
            "'consolidation')",
            name="ck_review_run_items_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','skipped','cancelled')",
            name="ck_review_run_items_status",
        ),
    )
    op.create_index(
        "ix_review_run_items_run",
        "review_run_items",
        ["review_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_run_items_run", table_name="review_run_items")
    op.drop_table("review_run_items")
    op.drop_index("uq_review_runs_active", table_name="review_runs")
    op.drop_index("ix_review_runs_ws_triggered", table_name="review_runs")
    op.drop_table("review_runs")
