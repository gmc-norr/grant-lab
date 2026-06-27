"""ranking_jobs query indexes for non-active rows

Revision ID: 0057
Revises: 0056

Adds two indexes that serve the daily ``reconcile_missing_rankings`` queries:

* ``ix_ranking_jobs_call_profile`` — covers the correlated NOT EXISTS lookup
  on (call_id, profile_id) for ``failed`` rows.  The partial-unique index
  ``uq_ranking_jobs_active_pair`` only covers pending/running rows, leaving
  the failed-row correlated subquery and ``_ranking_counts`` JOIN on
  (call_id, profile_id) without an index.

* ``ix_ranking_jobs_profile_status`` — covers the (profile_id, status)
  grouping / filtering used by ``_ranking_counts`` and the per-profile
  status breakdown query.  The existing ``ix_ranking_jobs_status_updated``
  index is (status, updated_at) — it does not help when filtering by
  profile_id first.
"""

from __future__ import annotations

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_ranking_jobs_call_profile",
        "ranking_jobs",
        ["call_id", "profile_id"],
    )
    op.create_index(
        "ix_ranking_jobs_profile_status",
        "ranking_jobs",
        ["profile_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_ranking_jobs_profile_status", table_name="ranking_jobs")
    op.drop_index("ix_ranking_jobs_call_profile", table_name="ranking_jobs")
