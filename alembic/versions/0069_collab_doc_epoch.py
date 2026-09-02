"""collab_artifact_state.doc_epoch — a truncated log is a new document

Revision ID: 0069
Revises: 0068
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so every existing row is epoch 0 (a real value the route
    # can compare against); NOT NULL so no code path has to consider None.
    op.add_column(
        "collab_artifact_state",
        sa.Column(
            "doc_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("collab_artifact_state", "doc_epoch")
