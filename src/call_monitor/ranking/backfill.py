from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from call_monitor.db.models import Profile

_logger = logging.getLogger(__name__)
BACKFILL_DAYS = 90

# Bulk INSERT ... SELECT ... ON CONFLICT DO NOTHING: one round-trip per
# profile instead of N (~600 calls × per-row INSERT was the v0 pattern,
# which could block the admin POST handler for ~10s and trip a reverse
# proxy timeout). The ON CONFLICT predicate must MATCH the partial
# unique index byte-for-byte, otherwise PG can't use it.
_BULK_ENQUEUE_SQL = text(
    """
    INSERT INTO ranking_jobs (call_id, profile_id, scheme, status, attempts)
    SELECT c.id, :profile_id, 'v1', 'pending', 0
    FROM calls c
    -- NOTE: WHERE published_at >= :cutoff also excludes calls with
    -- published_at IS NULL (NULL comparisons return NULL, treated as
    -- false). In practice these are rare — most ingestion paths set
    -- published_at — but a profile created later will not see them.
    -- If we ever surface unranked calls to a new profile, this filter
    -- needs revisiting.
    WHERE c.published_at >= :cutoff
    ON CONFLICT (call_id, profile_id, scheme) WHERE status IN ('pending', 'running')
    DO NOTHING
    """
)


def backfill_profile(*, session_factory: sessionmaker, profile_id: int) -> int:
    """Enqueue ranking jobs for every Call from the last BACKFILL_DAYS × this
    profile, in one bulk statement. Returns the count newly enqueued
    (conflicts are silently skipped by ON CONFLICT DO NOTHING).
    """
    cutoff = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)
    with session_factory() as session:
        profile = session.get(Profile, profile_id)
        if profile is None or not profile.is_active:
            return 0
        result = session.execute(
            _BULK_ENQUEUE_SQL,
            {"profile_id": profile_id, "cutoff": cutoff},
        )
        enqueued = result.rowcount
        session.commit()
    _logger.info(
        "ranking.backfill_done",
        extra={
            "event": "ranking_backfill_done",
            "profile_id": profile_id,
            "enqueued": enqueued,
        },
    )
    return enqueued
