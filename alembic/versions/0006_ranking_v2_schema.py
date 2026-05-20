"""ranking v2: dual-scheme schema, criterion table, profile additions

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-17
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- call_rankings: add scheme + new columns, swap PK + index ---
    op.add_column(
        "call_rankings",
        sa.Column("scheme", sa.String(length=8), nullable=False, server_default=sa.text("'v1'")),
    )
    op.add_column(
        "call_rankings",
        sa.Column("aggregate_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "call_rankings",
        sa.Column(
            "setwise_offset",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column("call_rankings", "score", new_column_name="aggregate_score")

    op.drop_constraint("pk_call_rankings", "call_rankings", type_="primary")
    op.create_primary_key("pk_call_rankings", "call_rankings", ["call_id", "profile_id", "scheme"])

    op.drop_index("ix_call_rankings_profile_score", table_name="call_rankings")
    op.create_index(
        "ix_call_rankings_profile_scheme_score",
        "call_rankings",
        ["profile_id", "scheme", sa.text("aggregate_score DESC NULLS LAST")],
        postgresql_using="btree",
    )

    # --- ranking_jobs: add scheme, swap partial unique index ---
    op.add_column(
        "ranking_jobs",
        sa.Column("scheme", sa.String(length=8), nullable=False, server_default=sa.text("'v1'")),
    )
    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs",
        ["call_id", "profile_id", "scheme"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    # --- profiles: ranking_scheme + budget_currency ---
    op.add_column(
        "profiles",
        sa.Column(
            "ranking_scheme",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
    )
    op.add_column(
        "profiles",
        sa.Column("budget_currency", sa.String(length=3), nullable=True),
    )

    # --- new table: call_ranking_criteria ---
    op.create_table(
        "call_ranking_criteria",
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.BigInteger(), nullable=False),
        sa.Column("scheme", sa.String(length=8), nullable=False),
        sa.Column("criterion", sa.String(length=20), nullable=False),
        sa.Column("rating", sa.String(length=20), nullable=False),
        sa.Column("rating_expected", sa.Float(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("quoted_evidence", sa.Text(), nullable=True),
        sa.Column(
            "ranked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "call_id",
            "profile_id",
            "scheme",
            "criterion",
            name="pk_call_ranking_criteria",
        ),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["calls.id"],
            ondelete="CASCADE",
            name="fk_call_ranking_criteria_call",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            ondelete="CASCADE",
            name="fk_call_ranking_criteria_profile",
        ),
    )


def downgrade() -> None:
    """Reverse with documented data loss.

    Once dual-scheme rows have been written, restoring the old PK requires
    deleting one scheme. We delete v2 and keep v1 — the spec's documented
    point of no return for v2 ranking data."""
    op.drop_table("call_ranking_criteria")

    op.execute("DELETE FROM call_rankings WHERE scheme = 'v2'")
    op.execute("DELETE FROM ranking_jobs WHERE scheme = 'v2'")

    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs",
        ["call_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.drop_column("ranking_jobs", "scheme")

    op.drop_column("profiles", "budget_currency")
    op.drop_column("profiles", "ranking_scheme")

    # ORDER MATTERS: drop the new index + PK + rename column BEFORE recreating
    # the old index that references the old column name.
    op.drop_index("ix_call_rankings_profile_scheme_score", table_name="call_rankings")
    op.drop_constraint("pk_call_rankings", "call_rankings", type_="primary")
    op.create_primary_key("pk_call_rankings", "call_rankings", ["call_id", "profile_id"])
    op.alter_column("call_rankings", "aggregate_score", new_column_name="score")
    op.create_index(
        "ix_call_rankings_profile_score",
        "call_rankings",
        ["profile_id", sa.text("score DESC NULLS LAST")],
        postgresql_using="btree",
    )
    op.drop_column("call_rankings", "setwise_offset")
    op.drop_column("call_rankings", "aggregate_reason")
    op.drop_column("call_rankings", "scheme")
