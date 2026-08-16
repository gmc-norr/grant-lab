"""workspaces: lifecycle_generation, allocated per lifecycle transition

Revision ID: 0062
Revises: 0061
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so existing rows land at 0 without a backfill pass; NOT NULL
    # so no code path has to consider a null generation.
    op.add_column(
        "workspaces",
        sa.Column(
            "lifecycle_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "lifecycle_generation")
