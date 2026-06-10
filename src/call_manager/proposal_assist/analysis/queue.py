"""claim_analysis_jobs queue helpers. Follows the same patterns as
ranking/queue.py — ON CONFLICT dedup, FOR UPDATE SKIP LOCKED batch claim,
stuck-job reaping with attempt bumping and dead-lettering — but with
different attempt arithmetic: both queues bump attempts on reaper requeue;
the difference is that here attempts are bumped at CLAIM (MAX_ATTEMPTS=3),
whereas ranking bumps at FAILURE, in mark_failed (MAX_ATTEMPTS=5).
Spec 2026-06-10 §3."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from call_manager.db.models import ClaimAnalysisJob

MAX_ATTEMPTS = 3


def enqueue_section_job(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    section_index: int,
    section_heading: str,
    section_sha: str,
    commit_sha: str,
) -> bool:
    """Insert a queued job unless one already exists for this exact section
    content. Returns True if a row was inserted. Caller commits.

    Uses ON CONFLICT DO NOTHING against the partial unique index
    uq_claim_analysis_jobs_ws_sha_queued on (workspace_id, section_sha)
    WHERE status = 'queued'. The index_where predicate must match the
    migration's predicate verbatim so Postgres routes the conflict correctly.

    With psycopg3, rowcount is -1 because SQLAlchemy adds an implicit
    RETURNING for the server-generated PK — the driver treats the statement
    as a query rather than DML regardless of the ON CONFLICT outcome. Use
    inserted_primary_key instead: a non-None tuple on insert, None on
    conflict-skip. House precedent: ranking/queue.py lines 73-76.
    """
    stmt = (
        pg_insert(ClaimAnalysisJob)
        .values(
            workspace_id=workspace_id,
            section_index=section_index,
            section_heading=section_heading,
            section_sha=section_sha,
            commit_sha=commit_sha,
            status="queued",
        )
        .on_conflict_do_nothing(
            index_elements=["workspace_id", "section_sha"],
            index_where=text("status = 'queued'"),
        )
    )
    result = session.execute(stmt)
    return result.inserted_primary_key is not None


def claim_queued_jobs(session: Session, *, limit: int = 5) -> list[ClaimAnalysisJob]:
    """Atomically transition up to `limit` queued jobs to running and return
    them. Uses SELECT FOR UPDATE SKIP LOCKED for safe concurrent worker use.

    Heartbeat obligation: workers running long LLM loops MUST call
    touch_running_job between LLM calls, or the 15-minute stuck-job reaper
    may requeue a job that is still live and double-run it.

    Ordering: queued_at with id as tiebreaker — same-transaction enqueues all
    share queued_at (transaction-scoped now()), so id keeps the order
    deterministic.
    """
    rows = (
        session.query(ClaimAnalysisJob)
        .filter(ClaimAnalysisJob.status == "queued")
        .order_by(ClaimAnalysisJob.queued_at, ClaimAnalysisJob.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    for j in rows:
        j.status = "running"
        j.attempts += 1
        j.updated_at = datetime.now(UTC)
    session.flush()
    return rows


def touch_running_job(session: Session, job_id: int) -> None:
    """Heartbeat: refresh updated_at on a RUNNING job so the stuck-job reaper
    does not requeue live work. Workers must call this between long LLM calls
    (executes immediately on the caller's session — call inside the worker's
    per-job session; the caller's commit persists it). No-op if the job is no
    longer RUNNING.

    Row-lock note: this UPDATE takes a row lock on the job row that is HELD
    for the duration of the per-job transaction. requeue_stuck_running uses
    FOR UPDATE SKIP LOCKED, so an in-process reaper will skip a job whose
    row is already locked by an active per-job session. The timestamp
    update itself is uncommitted until job end and matters primarily across
    process restarts (not within the same process)."""
    session.query(ClaimAnalysisJob).filter_by(id=job_id, status="running").update(
        {"updated_at": datetime.now(UTC)}, synchronize_session=False
    )


def mark_done(session: Session, job: ClaimAnalysisJob) -> None:
    job.status = "done"
    # Clear any error left over from earlier failed attempts — stale errors
    # mislead admin views.
    job.last_error = None
    job.updated_at = datetime.now(UTC)


def mark_failed(session: Session, job: ClaimAnalysisJob, *, error: str) -> None:
    """Mark a job failed.

    If attempts < MAX_ATTEMPTS the job returns to 'queued' for retry —
    unless another queued row for the same (workspace_id, section_sha) was
    inserted while this job was running, which would violate the partial
    unique index. In that case the job is marked 'superseded' instead.

    At MAX_ATTEMPTS the job is dead-lettered to 'failed'.
    """
    job.last_error = error[:2000]
    job.updated_at = datetime.now(UTC)
    if job.attempts >= MAX_ATTEMPTS:
        job.status = "failed"
        return
    # READ COMMITTED snapshot: a concurrent enqueue can commit between this
    # check and our COMMIT, in which case the partial unique index raises
    # IntegrityError and rolls back the caller's transaction. Recovery: the
    # job stays running; the reaper picks it up later. Callers should commit
    # per-job, not per-batch.
    duplicate_queued = (
        session.query(ClaimAnalysisJob.id)
        .filter(
            ClaimAnalysisJob.workspace_id == job.workspace_id,
            ClaimAnalysisJob.section_sha == job.section_sha,
            ClaimAnalysisJob.status == "queued",
            ClaimAnalysisJob.id != job.id,
        )
        .first()
    )
    if duplicate_queued is not None:
        job.status = "superseded"
    else:
        job.status = "queued"


def mark_superseded(session: Session, job: ClaimAnalysisJob) -> None:
    job.status = "superseded"
    job.updated_at = datetime.now(UTC)


def requeue_stuck_running(session: Session, *, older_than_minutes: int = 15) -> int:
    """Reap stuck RUNNING jobs. Follows the same patterns as ranking/queue.py
    (SKIP LOCKED, attempt bumping, dead-lettering) but with different attempt
    arithmetic: both queues bump attempts here on requeue; the difference is
    that this queue also bumps at claim (MAX_ATTEMPTS=3), whereas ranking
    bumps at failure in mark_failed (MAX_ATTEMPTS=5). Exhausted jobs
    dead-letter to FAILED. A job whose content already has another QUEUED row
    (e.g. from invalidation re-enqueue) is marked superseded instead of queued,
    which would otherwise violate the partial unique index.

    Returns the number of jobs REQUEUED only — dead-lettered and superseded
    jobs are not counted."""
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    rows = (
        session.query(ClaimAnalysisJob)
        .filter(
            ClaimAnalysisJob.status == "running",
            ClaimAnalysisJob.updated_at < cutoff,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    n = 0
    for j in rows:
        j.attempts += 1
        j.updated_at = datetime.now(UTC)
        if j.attempts >= MAX_ATTEMPTS:
            j.status = "failed"
            j.last_error = (j.last_error or "") + " [reaped: attempts exhausted]"
            continue
        # READ COMMITTED snapshot: a concurrent enqueue between this check and
        # the caller's COMMIT aborts the whole reap batch via the unique
        # index; self-healing on the next tick (the jobs stay running and are
        # reaped again).
        # Note: this query relies on session autoflush to see earlier same-batch
        # transitions — do not wrap the reaper loop in no_autoflush.
        duplicate_queued = (
            session.query(ClaimAnalysisJob.id)
            .filter(
                ClaimAnalysisJob.workspace_id == j.workspace_id,
                ClaimAnalysisJob.section_sha == j.section_sha,
                ClaimAnalysisJob.status == "queued",
                ClaimAnalysisJob.id != j.id,
            )
            .first()
        )
        if duplicate_queued is not None:
            j.status = "superseded"
        else:
            j.status = "queued"
            n += 1
    return n
