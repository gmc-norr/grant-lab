"""Stuck-turn reaper.

A turn cancelled by the scheduler's `fut.result(timeout=300)` keeps running
on the app loop with its handle cached and the turn_jobs row stuck at
status='running'. The drain query excludes any workspace with a running row,
so that workspace is wedged forever — the orphan sweep only runs on a COLD
launch, which never happens for a wedged-but-cached workspace.

This reaper is a global, periodic sweep (NOT workspace-scoped, unlike
orphan_sweep): it marks any turn_jobs row that has been status='running'
longer than a TTL as 'failed', so the unique-running partial index frees up
and the next drain tick can proceed for that workspace.

The TTL must be safely longer than the longest legitimate streaming turn so
we never reap a turn that is still in flight. started_at is stamped at the
atomic claim (processor.process_turn_once), so it is the true wall-clock age
of the running turn.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import ChatTurn, TurnJob

logger = logging.getLogger(__name__)

# Default TTL: a running turn older than this is presumed wedged. 15 minutes
# is comfortably longer than the in-loop per-turn bound (settings.turn_max_seconds,
# default 600s / 10 min, enforced by drain via asyncio.wait_for). A slow-but-live
# turn is therefore cancelled+failed cleanly by that bound BEFORE this TTL fires,
# so the reaper only ever sees a TRULY wedged loop (cancellation itself didn't
# complete) — not a healthy long turn. Keep this comfortably above
# turn_max_seconds.
DEFAULT_REAP_TTL_SECONDS = 15 * 60


def reap_stuck_turns(
    *,
    session_factory: sessionmaker,
    ttl_seconds: int = DEFAULT_REAP_TTL_SECONDS,
    now: datetime | None = None,
) -> int:
    """Mark turn_jobs rows stuck in status='running' beyond ttl_seconds as
    'failed'. Returns the count of reaped rows.

    A row qualifies when status='running' AND started_at is older than
    (now - ttl_seconds). Rows with a NULL started_at are NOT reaped — a
    running row should always have started_at stamped at claim time, and
    skipping NULLs avoids reaping a row mid-claim by a racing transaction.
    """
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(seconds=ttl_seconds)

    with session_factory() as s:
        # Collect the stuck rows first so we can also clear the submit-time
        # tokens_in pre-count on their linked user-turn rows. submit_user_turn
        # pre-stamps the USER chat_turns row with an estimate (attachment vision
        # tokens + len(text)//4) that counts against the daily cap; only the
        # finalize paths clear it. The reaper bypasses finalize, so without this
        # a reaped (esp. vision) turn would permanently burn cap budget for
        # input it never actually consumed.
        stuck = s.execute(
            select(TurnJob.id, TurnJob.user_turn_id).where(
                TurnJob.status == "running",
                TurnJob.started_at.is_not(None),
                TurnJob.started_at < cutoff,
            )
        ).all()
        reaped = len(stuck)
        if reaped:
            job_ids = [row.id for row in stuck]
            user_turn_ids = [row.user_turn_id for row in stuck if row.user_turn_id is not None]
            s.execute(
                update(TurnJob)
                .where(TurnJob.id.in_(job_ids))
                .values(
                    status="failed",
                    error=f"reaped: stuck in running > {ttl_seconds}s (workspace wedged)",
                    completed_at=current,
                )
            )
            if user_turn_ids:
                s.execute(
                    update(ChatTurn).where(ChatTurn.id.in_(user_turn_ids)).values(tokens_in=None)
                )
        s.commit()

    if reaped:
        logger.warning(
            "turn_reaper: marked %d stuck running turn(s) as failed",
            reaped,
            extra={"event": "turn_reaper_reaped", "count": reaped},
        )
    return reaped
