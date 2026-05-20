"""Drop v1 ranking + setwise machinery

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-18

After Plan D, v2 is strictly stronger than v1 on every dimension. Plan E
removes the dual-scheme machinery (no production v1 data to preserve),
the setwise tie-break (point in time when borderline noise was worth
refining; the embedding pre-filter from Plan D made it superfluous),
and the persisted G-Eval logprob expected value (refines an integer
rating invisibly in the UI; not worth the parser fragility).

Schema deltas:
- profiles: drop ranking_scheme
- ranking_jobs: drop scheme; rebuild uq_ranking_jobs_active_pair without it
- call_rankings: drop scheme from PK (pk_call_rankings becomes
    (call_id, profile_id)); rebuild ix_call_rankings_profile_scheme_score as
    ix_call_rankings_profile_score; drop setwise_offset
- call_ranking_criteria: drop scheme from PK (pk_call_ranking_criteria
    becomes (call_id, profile_id, criterion)); drop rating_expected
- delete any rows where scheme='v1' (defensive — should be 0 in dev)

All constraint/index names checked against alembic/versions/0006_ranking_v2_schema.py.
The criterion table's PK column is `criterion` (not `name`), per the v2
criterion-design spec.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -------- ranking_jobs --------
    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.execute("DELETE FROM ranking_jobs WHERE scheme = 'v1'")
    op.drop_column("ranking_jobs", "scheme")
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs",
        ["call_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    # -------- call_rankings --------
    op.drop_index("ix_call_rankings_profile_scheme_score", table_name="call_rankings")
    op.execute("DELETE FROM call_rankings WHERE scheme = 'v1'")
    op.drop_constraint("pk_call_rankings", "call_rankings", type_="primary")
    op.create_primary_key("pk_call_rankings", "call_rankings", ["call_id", "profile_id"])
    op.drop_column("call_rankings", "scheme")
    op.drop_column("call_rankings", "setwise_offset")
    op.create_index(
        "ix_call_rankings_profile_score",
        "call_rankings",
        ["profile_id", sa.text("aggregate_score DESC NULLS LAST")],
        postgresql_using="btree",
    )

    # -------- call_ranking_criteria --------
    op.execute("DELETE FROM call_ranking_criteria WHERE scheme = 'v1'")
    op.drop_constraint("pk_call_ranking_criteria", "call_ranking_criteria", type_="primary")
    op.create_primary_key(
        "pk_call_ranking_criteria",
        "call_ranking_criteria",
        ["call_id", "profile_id", "criterion"],
    )
    op.drop_column("call_ranking_criteria", "scheme")
    op.drop_column("call_ranking_criteria", "rating_expected")

    # -------- profiles --------
    op.drop_column("profiles", "ranking_scheme")


def downgrade() -> None:
    # Best-effort downgrade — restores columns but cannot recreate v1 data.
    op.add_column(
        "profiles",
        sa.Column(
            "ranking_scheme",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'v2'"),
        ),
    )
    op.alter_column("profiles", "ranking_scheme", server_default=None)

    # call_ranking_criteria
    op.add_column(
        "call_ranking_criteria",
        sa.Column("rating_expected", sa.Float(), nullable=True),
    )
    op.add_column(
        "call_ranking_criteria",
        sa.Column(
            "scheme",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'v2'"),
        ),
    )
    op.alter_column("call_ranking_criteria", "scheme", server_default=None)
    op.drop_constraint("pk_call_ranking_criteria", "call_ranking_criteria", type_="primary")
    op.create_primary_key(
        "pk_call_ranking_criteria",
        "call_ranking_criteria",
        ["call_id", "profile_id", "scheme", "criterion"],
    )

    # call_rankings
    op.drop_index("ix_call_rankings_profile_score", table_name="call_rankings")
    op.add_column(
        "call_rankings",
        sa.Column(
            "setwise_offset",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.alter_column("call_rankings", "setwise_offset", server_default=None)
    op.add_column(
        "call_rankings",
        sa.Column(
            "scheme",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'v2'"),
        ),
    )
    op.alter_column("call_rankings", "scheme", server_default=None)
    op.drop_constraint("pk_call_rankings", "call_rankings", type_="primary")
    op.create_primary_key("pk_call_rankings", "call_rankings", ["call_id", "profile_id", "scheme"])
    op.create_index(
        "ix_call_rankings_profile_scheme_score",
        "call_rankings",
        ["profile_id", "scheme", sa.text("aggregate_score DESC NULLS LAST")],
        postgresql_using="btree",
    )

    # ranking_jobs
    op.drop_index("uq_ranking_jobs_active_pair", table_name="ranking_jobs")
    op.add_column(
        "ranking_jobs",
        sa.Column(
            "scheme",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'v2'"),
        ),
    )
    op.alter_column("ranking_jobs", "scheme", server_default=None)
    op.create_index(
        "uq_ranking_jobs_active_pair",
        "ranking_jobs",
        ["call_id", "profile_id", "scheme"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
