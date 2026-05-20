"""dismissals, digest runs, user bounce tracking

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_call_dismissals",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "call_id", name="pk_user_call_dismissals"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_user_call_dismissals_user",
        ),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["calls.id"],
            ondelete="CASCADE",
            name="fk_user_call_dismissals_call",
        ),
    )

    op.create_table(
        "digest_runs",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "included_call_ids",
            ARRAY(sa.BigInteger()),
            nullable=False,
            server_default=sa.text("'{}'::bigint[]"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_digest_runs"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_digest_runs_user",
        ),
    )
    op.create_index(
        "ix_digest_runs_user_sent",
        "digest_runs",
        ["user_id", sa.text("sent_at DESC")],
    )

    op.add_column(
        "users",
        sa.Column(
            "digest_bounce_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "digest_disabled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "digest_disabled_at")
    op.drop_column("users", "digest_bounce_count")
    op.drop_index("ix_digest_runs_user_sent", table_name="digest_runs")
    op.drop_table("digest_runs")
    op.drop_table("user_call_dismissals")
