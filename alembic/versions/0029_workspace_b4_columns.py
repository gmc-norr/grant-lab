"""workspace B4 columns: last_sdk_session_id + daily cap overrides + submitter

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("last_sdk_session_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("daily_token_cap_in_override", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("daily_token_cap_out_override", sa.Integer(), nullable=True),
    )
    op.add_column(
        "turn_jobs",
        sa.Column(
            "submitted_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("turn_jobs", "submitted_by_user_id")
    op.drop_column("workspaces", "daily_token_cap_out_override")
    op.drop_column("workspaces", "daily_token_cap_in_override")
    op.drop_column("workspaces", "last_sdk_session_id")
