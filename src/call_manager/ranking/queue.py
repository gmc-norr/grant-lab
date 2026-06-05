from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from call_manager.db.models import RankingJob, RankingJobStatus

_logger = logging.getLogger(__name__)
_LAST_ERROR_MAX = 2000

# A job that's been in RUNNING longer than this is presumed dead — the
# worker process either crashed (SIGKILL, OOM, dev-server Ctrl-C) or
# something hangs indefinitely. The reaper requeues it as PENDING so the
# next tick retries. Set generously to avoid double-running a slow but
# legitimate Gemma call (single-instance Ollama on a 2080 Ti is well
# under 60s per ranking, but bursts of dozens at once could queue up).
_STUCK_AFTER = timedelta(minutes=5)

# How far back reconcile looks. Matches backfill_profile's BACKFILL_DAYS
# so a pair missed by the orchestrator gets caught within the same window
# a manual /admin/rerank would recover it over.
_RECONCILE_DAYS = 90


def enqueue_for_pair(session: Session, *, call_id: int, profile_id: int) -> bool:
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


def reconcile_missing_rankings(session: Session) -> int:
    """Enqueue ranking jobs for (recent call × active profile) pairs that
    have no CallRanking and no active RankingJob. Returns the number of
    jobs created.

    Closes the orchestrator recovery gap documented in orchestrator.py:
    if step 3 (enqueue) fails or the process is killed between step 1
    (calls inserted, source timestamp advanced) and step 3, the affected
    calls are ingested but never ranked. Without this job an admin has
    to manually click `/admin/rerank` to recover. Running this daily
    keeps the lag under 24h without requiring human intervention.

    SQL strategy: one bulk INSERT ... SELECT per tick. The `NOT EXISTS`
    on CallRanking skips pairs we've already ranked; the ON CONFLICT on
    the partial unique index (status IN ('pending','running')) skips
    pairs we're already about to rank. The predicate must match the
    index's WHERE clause verbatim or PG can't route the conflict.

    Excludes calls with NULL published_at (same as backfill_profile —
    we can't bound their age, so we defer to the manual-rerank path).
    """
    cutoff = datetime.now(UTC) - timedelta(days=_RECONCILE_DAYS)
    result = session.execute(
        text(
            """
            INSERT INTO ranking_jobs (call_id, profile_id, status)
            SELECT c.id, p.id, 'pending'
            FROM calls c
            CROSS JOIN profiles p
            WHERE p.is_active = true
              AND c.published_at >= :cutoff
              AND NOT EXISTS (
                SELECT 1 FROM call_rankings cr
                WHERE cr.call_id = c.id AND cr.profile_id = p.id
              )
            ON CONFLICT (call_id, profile_id)
              WHERE status IN ('pending', 'running') DO NOTHING
            """
        ),
        {"cutoff": cutoff},
    )
    n = result.rowcount or 0
    if n:
        _logger.info(
            "ranking.reconcile_enqueued",
            extra={"event": "ranking_reconcile_enqueued", "count": n},
        )
    return n


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


# Mirrors worker.MAX_ATTEMPTS (can't import it — worker imports this module,
# so the dependency must point one way). Kept in sync deliberately.
_MAX_ATTEMPTS = 5


def requeue_stuck_running(session: Session, *, max_attempts: int = _MAX_ATTEMPTS) -> int:
    """Recover jobs stuck in RUNNING longer than _STUCK_AFTER (worker crashed
    mid-tick and never reached mark_done/mark_failed). Returns the number of
    rows touched (requeued + dead-lettered). Called at the start of each drain
    tick — without this, claimed jobs sit in RUNNING forever, invisible to
    claim_pending_jobs (which only finds PENDING).

    Tradeoff (changed behaviour): we now BUMP `attempts` on every requeue and
    dead-letter (→ FAILED) any job that has already reached `max_attempts`,
    instead of cycling it back to PENDING unconditionally. The old behaviour
    never incremented attempts, on the theory that a crash isn't the job's
    fault — but that let a job which *reliably* crashes the worker (e.g. a
    payload that triggers an OOM or an unhandled native fault before
    mark_failed can run) loop through claim → crash → requeue forever, never
    dead-lettering. Counting the crash toward the threshold caps that at
    max_attempts.

    The cost: a transient infrastructure blip that kills the worker mid-tick
    (deploy restart, host OOM from a *neighbour* process) now also burns an
    attempt against every in-flight job, so an unlucky job could dead-letter
    a little sooner than it strictly deserves. That's an acceptable trade for
    bounding the pathological crash-loop — and the operator can re-enqueue a
    dead-lettered job via /admin/rerank.
    """
    cutoff = datetime.now(UTC) - _STUCK_AFTER
    now = datetime.now(UTC)

    # Jobs that have used up their attempts go to the dead-letter (FAILED)
    # terminal state rather than back to PENDING — this is what stops the
    # crash-loop. attempts is still bumped so the recorded count is honest.
    dead = session.execute(
        update(RankingJob)
        .where(
            RankingJob.status == RankingJobStatus.RUNNING,
            RankingJob.updated_at < cutoff,
            RankingJob.attempts + 1 >= max_attempts,
        )
        .values(
            status=RankingJobStatus.FAILED,
            attempts=RankingJob.attempts + 1,
            last_error="dead-lettered: stuck RUNNING past max_attempts (worker crash loop)",
            updated_at=now,
        )
    )
    # Everything else still under the threshold goes back to PENDING for retry,
    # with attempts bumped so it eventually reaches the branch above.
    requeued = session.execute(
        update(RankingJob)
        .where(
            RankingJob.status == RankingJobStatus.RUNNING,
            RankingJob.updated_at < cutoff,
        )
        .values(
            status=RankingJobStatus.PENDING,
            attempts=RankingJob.attempts + 1,
            updated_at=now,
        )
    )
    n_dead = dead.rowcount or 0
    n_requeued = requeued.rowcount or 0
    if n_dead or n_requeued:
        _logger.warning(
            "ranking.requeued_stuck",
            extra={
                "event": "ranking_requeued_stuck",
                "count": n_requeued + n_dead,
                "requeued": n_requeued,
                "dead_lettered": n_dead,
            },
        )
    return n_requeued + n_dead


def mark_done(session: Session, job_id: int) -> None:
    session.execute(
        update(RankingJob)
        .where(RankingJob.id == job_id)
        .values(status=RankingJobStatus.DONE, updated_at=datetime.now(UTC))
    )


def mark_failed(session: Session, job_id: int, *, error: str, terminal: bool) -> None:
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
