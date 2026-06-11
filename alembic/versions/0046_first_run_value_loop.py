"""First-run value loop: profile_bootstrap_jobs + users.default_profile_id.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-11

Linear revision after ranking-v2.1 (0045) merged to master.
See docs/superpowers/plans/2026-06-11-first-run-value-loop.md Task 1.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profile_bootstrap_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("seed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pubmed_status", sa.Text(), nullable=True),
        sa.Column("publications_fetched", sa.Integer(), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("profile_id", name="uq_profile_bootstrap_jobs_profile"),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_profile_bootstrap_jobs_status",
        ),
    )
    op.add_column("users", sa.Column("default_profile_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_users_default_profile_id",
        "users",
        "profiles",
        ["default_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_default_profile_id", "users", type_="foreignkey")
    op.drop_column("users", "default_profile_id")
    op.drop_table("profile_bootstrap_jobs")
