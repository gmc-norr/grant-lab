"""users and magic_link_tokens

Revision ID: 0001
Revises:
Create Date: 2026-04-14
"""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
        # CHECK forces lowercase storage so UNIQUE(email) actually means
        # one row per address (Postgres `=` is case-sensitive otherwise).
        sa.CheckConstraint(
            "email = lower(email)", name="ck_users_email_lowercase"
        ),
    )
    op.create_table(
        "magic_link_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_mlt_user_id"
        ),
        sa.UniqueConstraint("token_hash", name="uq_mlt_token_hash"),
    )
    # Postgres auto-creates a unique index backing uq_mlt_token_hash, which
    # satisfies the "fast lookup by token hash" verification query. No extra
    # index needed.


def downgrade() -> None:
    op.drop_table("magic_link_tokens")
    op.drop_table("users")
