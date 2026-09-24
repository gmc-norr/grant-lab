"""ranking freshness: stamp what a ranking describes, track its second reading

Revision ID: 0081
Revises: 0080
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Spec D1. NULL means stale for both fingerprints: no grandfathering,
    # because one re-rank of the open feed is one ordinary night of work.
    op.add_column("call_rankings", sa.Column("call_fingerprint", sa.Text(), nullable=True))
    op.add_column("call_rankings", sa.Column("scoring_fingerprint", sa.Text(), nullable=True))
    # Spec D6. NULL = nothing outstanding.
    op.add_column("call_rankings", sa.Column("escalation_state", sa.Text(), nullable=True))
    op.add_column(
        "call_rankings",
        sa.Column(
            "escalation_failures", sa.SmallInteger(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.create_check_constraint(
        "ck_call_rankings_escalation_state",
        "call_rankings",
        "escalation_state IN ('pending','gave_up')",
    )
    # Spec D7: what a failed job failed ON. NULL on every pre-0081 row, which
    # keeps today's pair-wide 7-day suppression for those rows.
    for col in ("profile_fingerprint", "call_fingerprint", "scoring_fingerprint"):
        op.add_column("ranking_jobs", sa.Column(col, sa.Text(), nullable=True))
    # Spec D7 amendment (final review I1): WHY a job failed. A 'parse' failure
    # (deterministic at temperature 0) suppresses its pair on the same inputs;
    # a 'crash' (the reaper's dead-letter of a job that crashed the worker
    # MAX_ATTEMPTS times) suppresses it pair-wide; 'service' (the model
    # service) and 'error' (anything else) never do. NULL on every pre-0081
    # row, which keeps the old rule.
    op.add_column("ranking_jobs", sa.Column("failure_kind", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_ranking_jobs_failure_kind",
        "ranking_jobs",
        "failure_kind IS NULL OR failure_kind IN ('parse','service','error','crash')",
    )
    # Spec D8: the writer no longer produces a timeline criterion row. Closed
    # pairs are never re-ranked, so without this their stale chip would stay.
    op.execute("DELETE FROM call_ranking_criteria WHERE criterion = 'timeline'")


def downgrade() -> None:
    # The deleted timeline rows are not restored: they were derived data, and
    # the next re-rank on the old code writes them again.
    op.drop_constraint("ck_ranking_jobs_failure_kind", "ranking_jobs", type_="check")
    op.drop_column("ranking_jobs", "failure_kind")
    for col in ("scoring_fingerprint", "call_fingerprint", "profile_fingerprint"):
        op.drop_column("ranking_jobs", col)
    op.drop_constraint("ck_call_rankings_escalation_state", "call_rankings", type_="check")
    op.drop_column("call_rankings", "escalation_failures")
    op.drop_column("call_rankings", "escalation_state")
    op.drop_column("call_rankings", "scoring_fingerprint")
    op.drop_column("call_rankings", "call_fingerprint")
