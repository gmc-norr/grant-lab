"""golden_set_triples — DB-backed curated eval set.

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-11

See docs/superpowers/plans/2026-06-11-ranking-v2.1-improvements.md Task 6.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "golden_set_triples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("expected_score", sa.SmallInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("call_id", "profile_id", name="uq_golden_set_triples_pair"),
        sa.CheckConstraint(
            "expected_score BETWEEN 0 AND 100", name="ck_golden_set_triples_score_range"
        ),
    )


def downgrade() -> None:
    op.drop_table("golden_set_triples")
