"""source failure streak + last_alerted_at

Revision ID: 0056
Revises: 0055

Persists consecutive-failure streak and last-alerted timestamp on sources so
the alerter survives process restarts and OOM kills. Together with the
stale-fetch backstop these two columns make the alerter restart-independent.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "sources",
        sa.Column(
            "last_alerted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("sources", "last_alerted_at")
    op.drop_column("sources", "consecutive_failures")
