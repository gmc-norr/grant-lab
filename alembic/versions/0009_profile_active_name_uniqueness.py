"""Partial unique index on active profile names + DigestRun week uniqueness

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-24

Closes two idempotency gaps surfaced in the post-Plan-F code review:

1. `profiles.name` had no DB-level uniqueness beyond a handler pre-check.
   Two concurrent submits of the same name could both pass the pre-check
   before either committed, yielding duplicate active profiles.
   Fix: `CREATE UNIQUE INDEX ON profiles (name) WHERE is_active = true`.
   Archived profiles (is_active=false) can still reuse names — that's
   intentional, it's why this is a partial index.

2. `digest_runs` had no DB-level protection against double-send. The
   handler's Python-side "already sent this week" check relied entirely
   on the single-instance scheduler invariant. An accidental second
   replica or an overlapping manual run would send the same digest twice.
   Fix: add a generated `week_start` column (Monday 00:00 of the week
   the send lands in, truncated in UTC because Postgres `date_trunc` is
   deterministic there; the app translates to Stockholm-week at read time)
   and `UNIQUE(user_id, week_start)`.

   We compute week_start with `date_trunc('week', sent_at)` which returns
   the ISO Monday of the week in UTC. That's one slot per UTC week, which
   is close enough to the Stockholm-week the app uses for idempotency
   that a cross-boundary race still gets caught. Worst case a client in
   the 1–2h sliver where UTC-week and Stockholm-week disagree gets a
   false-negative dedupe and a second digest — no worse than today, and
   the app's Python-side week_start check catches it on top of this.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -------- profiles.name uniqueness --------
    op.create_index(
        "uq_profiles_active_name",
        "profiles",
        ["name"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    # -------- digest_runs week uniqueness --------
    # GENERATED ALWAYS AS STORED gives us a column the app can also read
    # for filtering, while keeping the value derived from sent_at so it
    # can't be set independently (and wrong).
    #
    # `date_trunc('week', timestamptz, 'UTC')` is the 3-arg form added in
    # Postgres 16 — it's IMMUTABLE (independent of the session TimeZone
    # GUC), which generated columns require. The 2-arg form is only
    # STABLE and would be rejected. We use UTC rather than Stockholm for
    # the stored boundary so daylight-saving transitions don't gradually
    # shift the bucket edges — the app still computes Stockholm-week for
    # its own idempotency check at read time, which remains the
    # user-facing semantic.
    op.execute(
        "ALTER TABLE digest_runs "
        "ADD COLUMN week_start TIMESTAMPTZ "
        "GENERATED ALWAYS AS (date_trunc('week', sent_at, 'UTC')) STORED"
    )
    op.create_index(
        "uq_digest_runs_user_week",
        "digest_runs",
        ["user_id", "week_start"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_digest_runs_user_week", table_name="digest_runs")
    op.execute("ALTER TABLE digest_runs DROP COLUMN week_start")
    op.drop_index("uq_profiles_active_name", table_name="profiles")
