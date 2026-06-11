"""call_rankings.topic_fit_expected — G-Eval expected value.

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-11

See docs/superpowers/plans/2026-06-11-ranking-v2.1-improvements.md Task 16.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call_rankings",
        sa.Column("topic_fit_expected", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("call_rankings", "topic_fit_expected")
