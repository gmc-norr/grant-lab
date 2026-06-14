"""users.status + oidc identity columns

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable first so we can backfill pre-existing (trusted) rows to
    # 'active', THEN set the going-forward default to 'pending' (fail closed).
    op.add_column("users", sa.Column("status", sa.String(length=16), nullable=True))
    # Fail-SAFE backfill. The OLD magic-link flow auto-created a row for ANY
    # allowed-domain email that merely submitted the form (even if the link was
    # never clicked), so blanket-activating every row would let never-confirmed
    # addresses bypass the new pending->approve gate this feature adds. Activate
    # only rows that are deliberate accounts:
    #   - last_login_at set  -> the email link was actually clicked (confirmed), OR
    #   - is_admin           -> a deliberately-granted privileged account (don't
    #                           lock out a manually-promoted admin who hasn't
    #                           logged in yet; INITIAL_ADMIN_EMAIL admins would be
    #                           re-activated by bootstrap, but manually-granted
    #                           ones would not).
    # Everything else (never-confirmed auto-provisioned rows) falls to 'pending'.
    op.execute(
        "UPDATE users SET status = 'active' "
        "WHERE status IS NULL AND (last_login_at IS NOT NULL OR is_admin)"
    )
    op.execute("UPDATE users SET status = 'pending' WHERE status IS NULL")
    op.alter_column("users", "status", server_default="pending", nullable=False)

    op.add_column("users", sa.Column("oidc_iss", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("oidc_sub", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("approved_by_user_id", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_users_approved_by",
        "users",
        "users",
        ["approved_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_users_status_valid", "users", "status IN ('pending', 'active', 'disabled')"
    )
    op.create_check_constraint(
        "ck_users_oidc_paired_null", "users", "(oidc_iss IS NULL) = (oidc_sub IS NULL)"
    )
    op.create_index(
        "uq_users_oidc_identity",
        "users",
        ["oidc_iss", "oidc_sub"],
        unique=True,
        postgresql_where=sa.text("oidc_sub IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_oidc_identity", table_name="users")
    op.drop_constraint("ck_users_oidc_paired_null", "users", type_="check")
    op.drop_constraint("ck_users_status_valid", "users", type_="check")
    op.drop_constraint("fk_users_approved_by", "users", type_="foreignkey")
    op.drop_column("users", "approved_at")
    op.drop_column("users", "approved_by_user_id")
    op.drop_column("users", "oidc_sub")
    op.drop_column("users", "oidc_iss")
    op.drop_column("users", "status")
