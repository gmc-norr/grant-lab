"""profiles, call_rankings, ranking_jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    profile_kind = postgresql.ENUM("org", "group", "user", name="profile_kind", create_type=False)
    profile_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "profiles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="profile_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "structured",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT", name="fk_profiles_created_by"
        ),
    )

    op.create_table(
        "call_rankings",
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column(
            "raw_response",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "sample_count",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "ranked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("call_id", "profile_id", name="pk_call_rankings"),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], ondelete="CASCADE", name="fk_call_rankings_call"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], ondelete="CASCADE", name="fk_call_rankings_profile"
        ),
    )
    # Score column declared DESC NULLS LAST so the dashboard's
    # ORDER BY score DESC NULLS LAST plan can use this index directly.
    op.create_index(
        "ix_call_rankings_profile_score",
        "call_rankings",
        ["profile_id", sa.text("score DESC NULLS LAST")],
    )

    job_status = postgresql.ENUM(
        "pending",
        "running",
        "failed",
        "done",
        name="ranking_job_status",
        create_type=False,
    )
    job_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ranking_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="ranking_job_status", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], ondelete="CASCADE", name="fk_ranking_jobs_call"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], ondelete="CASCADE", name="fk_ranking_jobs_profile"
        ),
    )
    op.create_index(
        "ix_ranking_jobs_status_updated",
        "ranking_jobs",
        ["status", "updated_at"],
    )
    # Partial unique: prevents stacking duplicate active jobs on the same pair.
    # Terminal states (done, failed) are excluded so a fresh re-rank can
    # coexist with prior history.
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs",
        ["call_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.drop_index("ix_ranking_jobs_status_updated", table_name="ranking_jobs")
    op.drop_table("ranking_jobs")
    op.execute("DROP TYPE ranking_job_status")
    op.drop_index("ix_call_rankings_profile_score", table_name="call_rankings")
    op.drop_table("call_rankings")
    op.drop_table("profiles")
    op.execute("DROP TYPE profile_kind")
