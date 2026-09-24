from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from grant_lab.db.models import RankingJob, RankingJobStatus
from grant_lab.ranking.freshness import FreshnessContext, enqueue_stale_pairs

_logger = logging.getLogger(__name__)
_LAST_ERROR_MAX = 2000

# A job that's been in RUNNING longer than this is presumed dead — the
# worker process either crashed (SIGKILL, OOM, dev-server Ctrl-C) or
# something hangs indefinitely. The reaper requeues it as PENDING so the
# next tick retries. Set generously to avoid double-running a slow but
# legitimate Gemma call: a batch of `batch_size` jobs is processed serially
# and each can take up to the per-call LLM timeout, so the worst-case batch
# wall-clock is batch_size × timeout. The worker also heartbeats updated_at
# as it starts each job (see touch_running_job), so a long batch keeps its
# in-flight jobs "fresh" and only a genuinely crashed/hung job ages past this.
_STUCK_AFTER = timedelta(minutes=15)


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


def bulk_enqueue_for_calls(session: Session, call_ids: list[int], *, ctx: FreshnessContext) -> int:
    """Enqueue the STALE (touched call x active profile) pairs after an ingest.

    Before ranking spec 2026-09-24 this enqueued every touched pair, and
    "touched" is every call the adapter returned -- so the whole open feed was
    re-ranked, and re-paid on Sonnet, every night. Now only a pair whose inputs
    changed, or whose second reading is owed, is enqueued (ranking/freshness.py).
    """
    if not call_ids:
        return 0
    return enqueue_stale_pairs(session, ctx=ctx, call_ids=call_ids)


def reconcile_missing_rankings(session: Session, *, ctx: FreshnessContext) -> int:
    """The daily 04:00 repair: enqueue every stale (open call x active profile)
    pair (ranking/freshness.py). Closes the orchestrator's step-1-succeeded-
    but-step-3-failed gap, catches pairs whose call vector caught up at 03:30,
    and drains owed escalations after the midnight cap reset."""
    n = enqueue_stale_pairs(session, ctx=ctx)
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


def touch_running_job(session: Session, job_id: int) -> None:
    """Heartbeat: bump a RUNNING job's updated_at to now so a long serial
    batch doesn't let the stuck-reaper mistake an in-flight job for a dead
    one. No-op if the job is no longer RUNNING (already finished/failed)."""
    session.execute(
        update(RankingJob)
        .where(RankingJob.id == job_id, RankingJob.status == RankingJobStatus.RUNNING)
        .values(updated_at=datetime.now(UTC))
    )


# Single source of truth for the retry cap (B11 Defect 5). Defined HERE
# because worker imports this module (the dependency points one way), so worker
# imports MAX_ATTEMPTS from here rather than re-declaring its own literal.
#
# Per spec: 5 attempts before dead-letter.
#
# Spec deviation (intentional): the spec describes exponential backoff
# (30s, 2m, 10m). We ship FLAT retries — a failed job goes back to PENDING and
# the next scheduler tick (60s default) retries it. So the effective wait is
# one tick per attempt, regardless of attempt index. Acceptable because failure
# rates are low (Ollama is local; the JSON-mode contract eliminates parse
# churn). True exponential backoff would require a `next_attempt_at` column on
# ranking_jobs and a WHERE clause on `claim_pending_jobs`.
MAX_ATTEMPTS = 5


def requeue_stuck_running(session: Session, *, max_attempts: int = MAX_ATTEMPTS) -> int:
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
            # This job crashed the worker max_attempts times: the crash loop
            # described above, which WILL recur. 'crash' suppresses the pair
            # pair-wide for DEAD_LETTER_SUPPRESS_DAYS (ranking spec D7
            # amendment, final review I1). No stamps: whatever the dead worker
            # captured died with it, so it cannot be keyed on inputs.
            failure_kind="crash",
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


def revert_to_pending(session: Session, job_ids: list[int]) -> int:
    """Return claimed-but-unprocessed RUNNING jobs to PENDING WITHOUT bumping
    attempts (B11 Defect 6). Used when a per-tick wall-clock budget is hit: the
    jobs were never actually attempted, so they must not burn a retry — they
    simply wait for the next tick. Scoped to RUNNING so a job that raced to a
    terminal state isn't resurrected. Returns the number of rows reverted."""
    if not job_ids:
        return 0
    result = session.execute(
        update(RankingJob)
        .where(
            RankingJob.id.in_(job_ids),
            RankingJob.status == RankingJobStatus.RUNNING,
        )
        .values(status=RankingJobStatus.PENDING, updated_at=datetime.now(UTC))
    )
    return result.rowcount or 0


def mark_done(session: Session, job_id: int) -> None:
    session.execute(
        update(RankingJob)
        .where(RankingJob.id == job_id)
        .values(status=RankingJobStatus.DONE, updated_at=datetime.now(UTC))
    )


# Why a ranking job failed (ranking spec D7 amendment, final review I1). The
# vocabulary of ranking_jobs.failure_kind (CHECK ck_ranking_jobs_failure_kind):
#   parse   -- the local model's output did not parse (ParseError). Deterministic
#              at temperature 0: suppresses its pair on the SAME inputs.
#   service -- the model service failed (LLMError: refused, timeout, HTTP error).
#              Not the pair's fault; never suppresses.
#   error   -- anything else. Never suppresses: a bug should stay visible, and a
#              transient database error should retry.
#   crash   -- requeue_stuck_running's dead-letter: the job crashed the worker
#              MAX_ATTEMPTS times and will again. Suppresses pair-wide (no stamps).
# mark_failed takes all but `crash`: a worker that reached it did not crash.
MARK_FAILED_KINDS = ("parse", "service", "error")


def mark_failed(
    session: Session,
    job_id: int,
    *,
    error: str,
    terminal: bool,
    failure_kind: str,
    stamps: tuple[str, str, str] | None = None,
) -> None:
    """Mark a job failed. If terminal=True, transition to the FAILED
    terminal status (dead-letter). Otherwise revert to PENDING with
    incremented attempts so the queue retries it on the next tick.

    `failure_kind` is one of MARK_FAILED_KINDS, written on EVERY call: the last
    failure's kind is what a FAILED row carries. NULL is reserved for rows
    written before migration 0081, which enqueue_stale_pairs reads (with NULL
    stamps) as the old pair-wide rule, and 'crash' (pair-wide too) is the
    reaper's alone, so None, 'crash' or an unknown kind raises ValueError
    before anything is written.

    `stamps` = (profile, call, scoring) fingerprints the job failed ON. Dead-
    letter suppression (ranking/freshness.py, spec D7) compares them with the
    pair's current inputs, so a corrected call is retried at once. None when
    the failure happened before they were captured.

    All three or none, never a partial set: enqueue_stale_pairs suppresses on a
    `parse` failure only when all three stamps equal the current inputs. A
    partial set never matches, so a deterministic parse failure would be
    retried every day. A partial or empty set raises ValueError before
    anything is written.

    `error` is truncated to `_LAST_ERROR_MAX` chars so a stray traceback
    doesn't blow up row size (the column is unbounded TEXT)."""
    if failure_kind not in MARK_FAILED_KINDS:
        raise ValueError(
            f"mark_failed needs a failure_kind in {MARK_FAILED_KINDS}, got {failure_kind!r}"
        )
    if stamps is not None and (
        len(stamps) != 3 or not all(isinstance(s, str) and s for s in stamps)
    ):
        raise ValueError(f"mark_failed needs all three stamps or none, got {stamps!r}")
    new_status = RankingJobStatus.FAILED if terminal else RankingJobStatus.PENDING
    values: dict[str, object] = {
        "status": new_status,
        "attempts": RankingJob.attempts + 1,
        "last_error": (error or "")[:_LAST_ERROR_MAX],
        "failure_kind": failure_kind,
        "updated_at": datetime.now(UTC),
    }
    if stamps is not None:
        values["profile_fingerprint"], values["call_fingerprint"], values["scoring_fingerprint"] = (
            stamps
        )
    session.execute(update(RankingJob).where(RankingJob.id == job_id).values(**values))
