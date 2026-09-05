"""claim_analysis_jobs queue helpers. Follows the same patterns as
ranking/queue.py — ON CONFLICT dedup, FOR UPDATE SKIP LOCKED batch claim,
stuck-job reaping with attempt bumping and dead-lettering — but with
different attempt arithmetic: both queues bump attempts on reaper requeue;
the difference is that here attempts are bumped at CLAIM (MAX_ATTEMPTS=3),
whereas ranking bumps at FAILURE, in mark_failed (MAX_ATTEMPTS=5).
Spec 2026-06-10 §3."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from grant_lab.db.models import ClaimAnalysisJob, Workspace
from grant_lab.proposal_assist.artifacts import PROPOSAL_FILENAME
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.row_locks import bounded_lock_waits

MAX_ATTEMPTS = 3

# mark_done's FOR UPDATE re-read runs in an APScheduler worker thread, and
# PostgreSQL's lock_timeout is 0 (disabled) unless a transaction sets it — see
# row_locks' module docstring. Unbounded, one stuck holder of the job row parks
# that worker thread for as long as it holds it.
#
# The wait is expected to be ~0: every other writer of a 'running' row takes it
# with SKIP LOCKED (claim_queued_jobs, requeue_stuck_running, and the jobs
# retirement hook's cancel), so this bounds the UNEXPECTED — a holder outside
# that set, or one added later — rather than the normal case.
#
# 2 seconds, the same value and the same reasoning as
# row_locks.PASS_PREFIX_LOCK_TIMEOUT_SECONDS: giving up is cheap on this path
# (drain_analysis_queue rolls the per-job transaction back and mark_failed
# requeues the job for another attempt), and waiting is not.
MARK_DONE_LOCK_TIMEOUT_SECONDS = 2

logger = logging.getLogger(__name__)


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


# A section is "covered" when a job for its exact content is queued, running,
# or done. FAILED is deliberately absent: a dead-lettered job's content gets
# another chance via the coverage loop (fresh row, fresh attempt budget).
# SUPERSEDED is likewise absent, but a superseded sha only reappears at HEAD
# via a content revert, which deserves a re-run anyway.
_COVERED_STATUSES = ("queued", "running", "done")


def enqueue_uncovered_sections(
    session: Session, *, workspace_id: uuid.UUID, doc: str, commit_sha: str
) -> int:
    """Enqueue a claim-analysis job for every non-empty section of *doc* whose
    content sha has no queued/running/done job. Content-keyed idempotent; dedups
    duplicate-content sections; performs no file/git IO (caller supplies the markdown) and
    does NOT commit (caller owns the transaction). Returns the number inserted.

    Callers: the collab checkpoint hook (3b-5d.D3 immediacy) and the reconcile
    tick (the always-on safety net) — one source of truth for the coverage
    predicate."""
    covered = set(
        session.execute(
            select(ClaimAnalysisJob.section_sha).where(
                ClaimAnalysisJob.workspace_id == workspace_id,
                ClaimAnalysisJob.status.in_(_COVERED_STATUSES),
            )
        ).scalars()
    )
    inserted = 0
    for sec in split_sections(doc):
        if not sec.text.strip():
            continue  # empty preamble/section — nothing to analyze
        if sec.sha in covered:
            continue
        if enqueue_section_job(
            session,
            workspace_id=workspace_id,
            section_index=sec.index,
            section_heading=sec.heading,
            section_sha=sec.sha,
            commit_sha=commit_sha,
        ):
            inserted += 1
        # Duplicate sections share content (and a sha) — enqueue once.
        covered.add(sec.sha)
    return inserted


def enqueue_containing_section(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    workspaces_dir: str,
    commit_sha: str,
    ev_id: str,
) -> None:
    """After a successful citation insert, enqueue a claim-analysis job for the
    section that now contains the new ev_id marker.

    Re-splits the HEAD document, locates the first section whose text contains
    the new marker, and calls enqueue_section_job in the SAME session
    (content-keyed ON CONFLICT DO NOTHING makes this idempotent). The
    reconciliation tick would self-heal eventually; this is immediacy.

    Errors are logged and swallowed — a failed re-enqueue must not roll back
    the committed insert.
    """
    marker_fragment = f"ev:{ev_id}"
    try:
        repo = workspace_repo_dir(workspaces_dir, workspace_id)
        proposal = repo / PROPOSAL_FILENAME
        if not proposal.exists():
            return
        sections = split_sections(proposal.read_text())
        for sec in sections:
            if marker_fragment in sec.text:
                enqueue_section_job(
                    session,
                    workspace_id=workspace_id,
                    section_index=sec.index,
                    section_heading=sec.heading,
                    section_sha=sec.sha,
                    commit_sha=commit_sha,
                )
                session.commit()
                return
    except Exception:
        logger.exception(
            "post-insert re-enqueue failed for workspace %s ev %s", workspace_id, ev_id
        )


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
    # Refuse to claim work for a deleted workspace — durable in SQL, so it survives
    # restart, exactly like the four pass claim queries. WHERE ... IN (subquery) is
    # not a join, so FOR UPDATE still locks only claim_analysis_jobs rows.
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    rows = (
        session.query(ClaimAnalysisJob)
        .filter(
            ClaimAnalysisJob.status == "queued",
            ClaimAnalysisJob.workspace_id.in_(live_ws),
        )
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
    """Terminalize a job as 'done' — but ONLY if the row is still 'running'.

    FOR UPDATE re-read rather than a bare attribute write, because this is not the
    only writer of the row. The "jobs" retirement hook terminalizes a deleted
    workspace's claim-analysis rows to
    status='failed'/last_error='cancelled: workspace deleted' in its own
    transaction (jobs_retirement.py), and it SKIP LOCKEDs past only the rows a
    worker is actually holding. A worker whose per-job session happens to hold no
    lock on this row — every sentence a cache hit, so touch_running_job never ran —
    would otherwise flush 'done' over the hook's 'failed' while leaving the
    cancellation message behind: a lost update, and an incoherent row that reads
    done-with-a-cancellation-error.

    Same guard, same reason, as the four LLM pass runners
    (coherence/pass_runner.py:142 "we must not resurrect it"). Returning without
    writing is correct here: the row is already terminal, so there is nothing left
    to finalize.

    BOUNDED (MARK_DONE_LOCK_TIMEOUT_SECONDS): raises RowLockTimeout rather than
    blocking a worker thread indefinitely on a lock nothing else bounds. The
    caller's generic handler rolls the per-job transaction back and requeues.

    Declining is NOT silent: the caller reads `job.status` to account for the
    outcome, so the stale in-memory 'running' is expired and re-read from the row
    this just locked. Left stale, a job terminalized under us is counted in no
    bucket at all while its annotation writes still commit.
    """
    with bounded_lock_waits(
        session,
        timeout_seconds=MARK_DONE_LOCK_TIMEOUT_SECONDS,
        message="timed out waiting for the claim-analysis job row",
    ):
        live = session.scalar(
            select(ClaimAnalysisJob.status).where(ClaimAnalysisJob.id == job.id).with_for_update()
        )
    if live != "running":
        # expire, not assignment: assigning would dirty the attribute and flush a
        # redundant UPDATE writing back the value we just read.
        session.expire(job, ["status"])
        return
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

    # A job whose workspace was deleted must NEVER return to 'queued': the claim
    # query filters deleted workspaces out, so a requeue strands it non-terminal
    # forever.
    #
    # LOCK ORDER: workspaces < claim_analysis_jobs (normative).
    #
    # FOR UPDATE, not a plain SELECT: a bare read is not a linearization point. It
    # can see 'live' while a DELETE commits moments later — the jobs hook then SKIP
    # LOCKEDs past this row because we hold it, and we commit the job back to
    # 'queued'. no_autoflush because `job` is already dirty here: an autoflush
    # would emit the job UPDATE first, taking the job row BEFORE the workspace row
    # and inverting the very order this block establishes.
    with session.no_autoflush:
        deleted = session.scalar(
            select(Workspace.soft_deleted_at)
            .where(Workspace.id == job.workspace_id)
            .with_for_update()
        )
    if deleted is not None:
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
