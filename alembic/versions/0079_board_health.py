"""board health: when the auto-publish job last tried, last succeeded, and why it failed

Revision ID: 0079
Revises: 0078
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All nullable with no server_default: NULL means "this board has never been
    # reconciled", which is the truth for every board that exists when this
    # lands, and a different statement from "reconciled at the epoch". The
    # read side must render it as "not yet", never as a date.
    op.add_column(
        "boards", sa.Column("last_reconcile_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "boards", sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("boards", sa.Column("last_reconcile_error", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("boards", "last_reconcile_error")
    op.drop_column("boards", "last_reconciled_at")
    op.drop_column("boards", "last_reconcile_attempt_at")
