"""Escalation columns + ranking_remote_attempts ledger.

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-11

See docs/superpowers/plans/2026-06-11-ranking-v2.1-improvements.md Task 21.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call_rankings",
        sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "call_rankings", sa.Column("escalated_from_score", sa.SmallInteger(), nullable=True)
    )
    op.add_column("call_rankings", sa.Column("analysis_model", sa.Text(), nullable=True))
    # Backfill: rows that already carry analysis prose were produced by the
    # model named in `model` (the old cosine-router only set summary on the
    # Sonnet path). Attribute accordingly.
    op.execute("UPDATE call_rankings SET analysis_model = model WHERE summary IS NOT NULL")

    op.create_table(
        "ranking_remote_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "outcome IN ('success','llm_error','parse_error')",
            name="ck_ranking_remote_attempts_outcome",
        ),
    )
    op.create_index(
        "ix_ranking_remote_attempts_created_at", "ranking_remote_attempts", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ranking_remote_attempts_created_at", table_name="ranking_remote_attempts")
    op.drop_table("ranking_remote_attempts")
    op.drop_column("call_rankings", "analysis_model")
    op.drop_column("call_rankings", "escalated_from_score")
    op.drop_column("call_rankings", "escalated")
