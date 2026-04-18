from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from call_monitor.db.models import RankingJob, RankingJobStatus

_logger = logging.getLogger(__name__)
_LAST_ERROR_MAX = 2000

# A job that's been in RUNNING longer than this is presumed dead — the
# worker process either crashed (SIGKILL, OOM, dev-server Ctrl-C) or
# something hangs indefinitely. The reaper requeues it as PENDING so the
# next tick retries. Set generously to avoid double-running a slow but
# legitimate Gemma call (single-instance Ollama on a 2080 Ti is well
# under 60s per ranking, but bursts of dozens at once could queue up).
_STUCK_AFTER = timedelta(minutes=5)


def enqueue_for_pair(
    session: Session, *, call_id: int, profile_id: int
) -> bool:
    """Enqueue a ranking job for the (call, profile) pair.

    Idempotent: ON CONFLICT DO NOTHING against the partial unique index
    on (call_id, profile_id) WHERE status IN ('pending', 'running').
    Returns True if a row was inserted, False if an active job already
    existed for the pair.

    Why ON CONFLICT (not try/except IntegrityError + rollback): callers
    typically run inside a larger transaction (the orchestrator wraps
    upsert + enqueue in one). A `session.rollback()` here would discard
    the surrounding work too. ON CONFLICT keeps the failure scoped to
    this single statement and never fails the surrounding transaction.

    Note on detection: psycopg3 sets rowcount=-1 for INSERT ... RETURNING
    regardless of whether a row was written or skipped. We use
    `inserted_primary_key` instead (non-None tuple on insert, None on skip).
    """
    stmt = (
        insert(RankingJob)
        .values(
            call_id=call_id,
            profile_id=profile_id,
            status=RankingJobStatus.PENDING,
        )
        .on_conflict_do_nothing(
            index_elements=["call_id", "profile_id"],
            # PG requires the predicate to match the partial unique index
            # (uq_ranking_jobs_active_pair) for the planner to route the
            # conflict resolution. Match the migration's predicate verbatim.
            index_where=text("status IN ('pending', 'running')"),
        )
    )
    result = session.execute(stmt)
    # With psycopg3 and INSERT ... RETURNING, rowcount is -1 regardless of
    # whether a row was inserted or skipped (the RETURNING clause causes the
    # driver to treat it as a query rather than DML). Use inserted_primary_key
    # instead: it is a non-None tuple on insert, None on conflict-skip.
    return result.inserted_primary_key is not None


def claim_pending_jobs(session: Session, *, limit: int) -> list[RankingJob]:
    """Atomically transition up to `limit` pending jobs into 'running' and
    return them. Uses SELECT FOR UPDATE SKIP LOCKED so concurrent workers
    (none today, but the queue model is forward-compatible) don't fight
    over the same rows.
    """
    stmt = (
        select(RankingJob)
        .where(RankingJob.status == RankingJobStatus.PENDING)
        .order_by(RankingJob.updated_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = session.execute(stmt).scalars().all()
    if not rows:
        return []
    now = datetime.now(UTC)
    for row in rows:
        row.status = RankingJobStatus.RUNNING
        row.updated_at = now
    session.flush()
    return rows


def requeue_stuck_running(session: Session) -> int:
    """Flip jobs that have been RUNNING longer than _STUCK_AFTER back to
    PENDING. Returns the number requeued. Called at the start of each
    drain tick — without this, a worker crash mid-tick leaves the
    claimed jobs in RUNNING forever (claim_pending_jobs only finds
    PENDING). Note: this does NOT increment `attempts`; the previous
    run never reached mark_failed so it shouldn't count toward the
    dead-letter threshold.
    """
    cutoff = datetime.now(UTC) - _STUCK_AFTER
    result = session.execute(
        update(RankingJob)
        .where(
            RankingJob.status == RankingJobStatus.RUNNING,
            RankingJob.updated_at < cutoff,
        )
        .values(status=RankingJobStatus.PENDING, updated_at=datetime.now(UTC))
    )
    n = result.rowcount or 0
    if n:
        _logger.warning(
            "ranking.requeued_stuck",
            extra={"event": "ranking_requeued_stuck", "count": n},
        )
    return n


def mark_done(session: Session, job_id: int) -> None:
    session.execute(
        update(RankingJob)
        .where(RankingJob.id == job_id)
        .values(status=RankingJobStatus.DONE, updated_at=datetime.now(UTC))
    )


def mark_failed(
    session: Session, job_id: int, *, error: str, terminal: bool
) -> None:
    """Mark a job failed. If terminal=True, transition to the FAILED
    terminal status (dead-letter). Otherwise revert to PENDING with
    incremented attempts so the queue retries it on the next tick.

    `error` is truncated to `_LAST_ERROR_MAX` chars so a stray traceback
    doesn't blow up row size (the column is unbounded TEXT)."""
    new_status = RankingJobStatus.FAILED if terminal else RankingJobStatus.PENDING
    session.execute(
        update(RankingJob)
        .where(RankingJob.id == job_id)
        .values(
            status=new_status,
            attempts=RankingJob.attempts + 1,
            last_error=(error or "")[:_LAST_ERROR_MAX],
            updated_at=datetime.now(UTC),
        )
    )
