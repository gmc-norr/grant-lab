"""drop magic_link_tokens

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("magic_link_tokens")


def downgrade() -> None:
    # Schema-only: this table is retired and has no live data. The recreated
    # table intentionally omits query-serving indexes (the table will never
    # be queried again in normal operation).
    op.create_table(
        "magic_link_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_mlt_user_id"
        ),
        sa.UniqueConstraint("token_hash", name="uq_mlt_token_hash"),
    )
