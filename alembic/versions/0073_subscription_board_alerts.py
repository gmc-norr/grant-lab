"""subscriptions.board_alerts_enabled

Revision ID: 0073
Revises: 0072
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "board_alerts_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    # NOT a round trip: dropping this destroys every owner's board-alert
    # preference, which a re-add cannot restore. Same reasoning as
    # 0070_profile_visibility.py's downgrade note.
    op.drop_column("subscriptions", "board_alerts_enabled")
