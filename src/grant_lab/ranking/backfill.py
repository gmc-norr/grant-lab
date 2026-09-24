from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from grant_lab.db.models import Profile

_logger = logging.getLogger(__name__)


def backfill_profile(*, session_factory: sessionmaker, profile_id: int) -> int:
    """Enqueue every OPEN call (deadline NULL or in the future) for this profile.

    No freshness check and no dead-letter suppression (ranking spec D4): backfill
    runs only after a human act. post_edit fires only when the profile's rank
    inputs changed, so every pair of this profile is stale already;
    bootstrap_worker runs for a profile with no rankings; and the admin
    "Re-rank everything" button is the override by definition (spec D5).

    Idempotent against the partial unique index per (call, profile).
    """
    with session_factory() as session:
        profile = session.get(Profile, profile_id)
        if profile is None or not profile.is_active:
            return 0
        result = session.execute(
            text(
                """
                INSERT INTO ranking_jobs (call_id, profile_id, status)
                SELECT c.id, :profile_id, 'pending'
                FROM calls c
                WHERE c.deadline IS NULL OR c.deadline > now()
                ON CONFLICT (call_id, profile_id)
                  WHERE status IN ('pending', 'running') DO NOTHING
                """
            ),
            {"profile_id": profile_id},
        )
        session.commit()
        enqueued = result.rowcount or 0
    _logger.info(
        "ranking.backfill_done",
        extra={"event": "ranking_backfill_done", "profile_id": profile_id, "enqueued": enqueued},
    )
    return enqueued
