from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from grant_lab.db.models import Profile, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import LEGACY_SENTINEL, rank_fingerprint

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

# How far back reconcile looks. Matches backfill_profile's BACKFILL_DAYS
# so a pair missed by the orchestrator gets caught within the same window
# a manual /admin/rerank would recover it over.
_RECONCILE_DAYS = 90

# How long a terminally-FAILED (dead-lettered) ranking_job suppresses
# reconcile/backfill re-enqueue of the same (call, profile) pair. Without
# this window a dead-lettered pair — which has no CallRanking and no active
# job — gets re-enqueued on every daily tick, burning attempts forever.
# Bounded (not forever) so a long-ago transient failure eventually retries.
_DEAD_LETTER_SUPPRESS_DAYS = 7


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


def bulk_enqueue_for_calls(session: Session, call_ids: list[int]) -> int:
    """Enqueue ranking jobs for every (call_id × active profile) pair in a
    SINGLE round-trip.  This replaces the N×M per-pair :func:`enqueue_for_pair`
    loop used by the ingestion orchestrator after Step 1.

    Uses ``CROSS JOIN unnest(:call_ids) AS cid`` so the whole fan-out is one
    INSERT statement, mirroring :func:`reconcile_missing_rankings`'s pattern.
    ON CONFLICT … DO NOTHING matches the partial unique index predicate so
    re-ingest of the same call doesn't produce duplicate jobs.

    Returns the number of rows actually inserted (0 when all pairs already had
    active jobs).  If ``call_ids`` is empty, returns 0 immediately.
    """
    if not call_ids:
        return 0
    result = session.execute(
        text(
            """
            INSERT INTO ranking_jobs (call_id, profile_id, status)
            SELECT cid, p.id, 'pending'
            FROM unnest(:call_ids) AS cid
            CROSS JOIN profiles p
            WHERE p.is_active = true
            ON CONFLICT (call_id, profile_id)
              WHERE status IN ('pending', 'running') DO NOTHING
            """
        ),
        {"call_ids": call_ids},
    )
    return result.rowcount or 0


def reconcile_missing_rankings(session: Session) -> int:
    """Enqueue ranking jobs for (recent call × active profile) pairs whose
    ranking is missing OR no longer current, and that have no active
    RankingJob. Returns the number of jobs created.

    "Current" means the stored CallRanking.profile_fingerprint still equals
    rank_fingerprint() recomputed from the profile row. A NULL fingerprint
    is stale: it cannot say what it describes.

    Closes the orchestrator recovery gap documented in orchestrator.py:
    if step 3 (enqueue) fails or the process is killed between step 1
    (calls inserted, source timestamp advanced) and step 3, the affected
    calls are ingested but never ranked. Without this job an admin has
    to manually click `/admin/rerank` to recover. Running this daily
    keeps the lag under 24h without requiring human intervention.

    SQL strategy: one bulk INSERT ... SELECT per tick. The `NOT EXISTS`
    on CallRanking skips pairs whose ranking is CURRENT — not merely pairs
    that have one. A ranking counts as current when its stored
    `profile_fingerprint` matches the fingerprint recomputed from the
    profile's present content, or when it predates migration 0075 and its
    profile has not been touched since. The ON CONFLICT on the partial
    unique index (status IN ('pending','running')) skips pairs we're
    already about to rank. The predicate must match the index's WHERE
    clause verbatim or PG can't route the conflict.

    Excludes calls with NULL published_at (same as backfill_profile —
    we can't bound their age, so we defer to the manual-rerank path).

    Also excludes pairs with a recent terminally-FAILED (dead-lettered)
    ranking_job. Such a pair has neither a CallRanking nor an active job,
    so without this guard reconcile would re-enqueue it every daily tick,
    burning attempts indefinitely. The suppression is time-bounded
    (`_DEAD_LETTER_SUPPRESS_DAYS`): a long-ago transient failure becomes
    eligible again rather than being abandoned forever. Operators can still
    force an immediate retry via /admin/rerank.
    """
    cutoff = datetime.now(UTC) - timedelta(days=_RECONCILE_DAYS)
    dead_letter_cutoff = datetime.now(UTC) - timedelta(days=_DEAD_LETTER_SUPPRESS_DAYS)

    # Fingerprints are Python over JSONB and cannot be computed in SQL, so the
    # active profiles come back first and their fingerprints ride in as a
    # VALUES list.
    # Column-limited, for the same reason Task 4 gives: `select(Profile)`
    # pulls the 1024-dim vector, and rank_fingerprint never reads it (it
    # reads description, structured, budget_currency, name, kind,
    # embedding_fingerprint and embedding_model). Task 4 asserts that
    # principle in gate 6, whose
    # scope is the EMBEDDING arm only -- so it does not cover this file, and
    # an earlier revision of this plan reintroduced the wholesale load here
    # three tasks later. `.all()`, not `.scalars()`: these are Rows.
    #
    # Verified: the Rows produce byte-identical rank_fingerprints to loaded
    # Profile objects (`kind` is String + CHECK, not a native enum, so both
    # sides give the same str through `getattr(x, "value", x)`), and the
    # emitted SQL carries no `profiles.embedding AS` column. That claim is no
    # longer only a comment: tests/test_ranking_queue_row_parity.py reads this
    # column list out of this function's source and executes it. Adding a key
    # to rank_fingerprint's payload without adding the column here reddens it.
    profiles = session.execute(
        select(
            Profile.id,
            Profile.name,
            Profile.kind,
            Profile.description,
            Profile.structured,
            Profile.budget_currency,
            Profile.embedding_fingerprint,
            Profile.embedding_model,
        ).where(Profile.is_active.is_(True))
    ).all()
    if not profiles:
        # An empty VALUES list is a SYNTAX ERROR. Today's CROSS JOIN degrades
        # to zero rows on an empty set; this early return preserves that.
        return 0

    values_sql = ", ".join(f"(:pid_{i}, :fp_{i})" for i in range(len(profiles)))
    params: dict[str, object] = {
        "cutoff": cutoff,
        "dead_letter_cutoff": dead_letter_cutoff,
        "legacy": LEGACY_SENTINEL,
    }
    for i, prof in enumerate(profiles):
        params[f"pid_{i}"] = prof.id
        params[f"fp_{i}"] = rank_fingerprint(prof)

    result = session.execute(
        text(
            f"""
            INSERT INTO ranking_jobs (call_id, profile_id, status)
            SELECT c.id, p.id, 'pending'
            FROM calls c
            CROSS JOIN (VALUES {values_sql}) AS want(profile_id, fingerprint)
            JOIN profiles p ON p.id = want.profile_id
            WHERE p.is_active = true
              AND c.published_at >= :cutoff
              -- Currency, not existence. A ranking counts as current when its
              -- fingerprint matches, OR when it predates migration 0075 AND
              -- its profile has not been touched since it was written.
              --
              -- `cr.ranked_at >= p.updated_at` is what BOUNDS the legacy
              -- exemption. An unconditional 'legacy' arm would exempt a
              -- pre-0075 pair forever, including after the edit whose
              -- background task was lost -- the exact case this repair exists
              -- for. Profile.updated_at carries onupdate=func.now(), so any
              -- edit revokes the exemption for that profile's pairs alone,
              -- and the row gets a real fingerprint afterwards so it never
              -- recurs.
              --
              -- A NULL fingerprint is NOT exempt: migration 0075 stamped every
              -- pre-existing row 'legacy', so a NULL here means a writer bug.
              AND NOT EXISTS (
                SELECT 1 FROM call_rankings cr
                WHERE cr.call_id = c.id AND cr.profile_id = p.id
                  AND ((cr.profile_fingerprint = :legacy
                        AND cr.ranked_at >= p.updated_at)
                       OR cr.profile_fingerprint = want.fingerprint)
              )
              AND NOT EXISTS (
                SELECT 1 FROM ranking_jobs rj
                WHERE rj.call_id = c.id
                  AND rj.profile_id = p.id
                  AND rj.status = 'failed'
                  AND rj.updated_at > :dead_letter_cutoff
              )
            ON CONFLICT (call_id, profile_id)
              WHERE status IN ('pending', 'running') DO NOTHING
            """
        ),
        params,
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
