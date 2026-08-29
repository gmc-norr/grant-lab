"""Pure-DB helpers for the coherence_jobs queue. The caller owns the transaction
(these flush, never commit), mirroring proposals/proposing.py + accepting.py."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, ProposedChange, Workspace
from call_manager.timeutil import stockholm_day_start_utc


def enqueue_coherence_run(
    session: Session, *, workspace_id: uuid.UUID, requested_by: int | None
) -> CoherenceJob:
    """Create a queued job, or return the existing active (queued|running) one.

    Race-safe against the uq_coherence_jobs_active partial-unique index: the INSERT
    happens INSIDE the SAVEPOINT (add + flush both inside begin_nested) so a
    unique-violation rolls back ONLY the savepoint, leaving the outer transaction
    usable for the re-read. (Adding before begin_nested risks the INSERT flushing
    outside the savepoint and poisoning the whole transaction.)
    """
    existing = session.scalar(
        select(CoherenceJob).where(
            CoherenceJob.workspace_id == workspace_id,
            CoherenceJob.status.in_(("queued", "running")),
        )
    )
    if existing is not None:
        return existing
    job = CoherenceJob(workspace_id=workspace_id, status="queued", requested_by=requested_by)
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        # Lost the race: another request inserted the active row first. The
        # savepoint rolled back, but `job` is restored to pending — expunge it so
        # the re-read's autoflush doesn't retry the doomed INSERT.
        if job in session:
            session.expunge(job)
        winner = session.scalar(
            select(CoherenceJob).where(
                CoherenceJob.workspace_id == workspace_id,
                CoherenceJob.status.in_(("queued", "running")),
            )
        )
        # Under READ COMMITTED the racer's active row is visible on re-read, so this
        # is unreachable in practice; guard anyway to keep the -> CoherenceJob contract honest.
        if winner is None:
            raise RuntimeError(
                "coherence enqueue lost the active-unique race but found no active job"
            ) from None
        return winner
    return job


def claim_next_coherence_job(session: Session) -> CoherenceJob | None:
    """Claim the oldest queued job whose workspace is LIVE and has no running job.
    Flips it to running + stamps started_at. FOR UPDATE SKIP LOCKED so parallel
    ticks don't fight. Soft-deleted workspaces are excluded so a pass can't burn
    tokens after deletion (the delete handler also cancels existing jobs).

    The join captures lifecycle_generation IN THE SAME STATEMENT as the FOR UPDATE
    lock (Global Constraint 2). A separate SELECT would reopen the delete/restore
    race the fence exists to close.

    `of=CoherenceJob` keeps the lock on the job row only. Without `of=`, PostgreSQL
    also locks the joined Workspace row, putting this claim in the delete path's way
    and inverting the lock order the architecture spec fixes in §5."""
    running_ws = select(CoherenceJob.workspace_id).where(CoherenceJob.status == "running")
    row = session.execute(
        select(CoherenceJob, Workspace.lifecycle_generation)
        .join(Workspace, Workspace.id == CoherenceJob.workspace_id)
        .where(
            CoherenceJob.status == "queued",
            CoherenceJob.workspace_id.not_in(running_ws),
            Workspace.soft_deleted_at.is_(None),
        )
        .order_by(CoherenceJob.queued_at)
        .limit(1)
        # `of=` keeps FOR UPDATE on the JOB only. Without it PostgreSQL would
        # also lock the Workspace row, putting this claim in the delete path's
        # way and inverting the lock order the architecture spec fixes in §5.
        .with_for_update(of=CoherenceJob, skip_locked=True)
    ).first()
    if row is None:
        return None
    job, generation = row
    job.status = "running"
    job.started_at = datetime.now(UTC)
    job.claimed_at_generation = generation
    session.flush()
    return job


def cancel_coherence_jobs_for_workspace(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Fail any active (queued|running) coherence job for a workspace — called from
    the soft-delete handler so a queued/running pass can't spend tokens after the
    workspace is deleted. Also supersedes each cancelled job's partial pending rows.

    The transition is ONE atomic UPDATE ... WHERE status IN (...) RETURNING (not an
    ORM load-then-write keyed on id) so it can't clobber a job the success finalizer
    is concurrently committing to 'completed': the UPDATE blocks on that row's
    FOR UPDATE lock, then re-evaluates the WHERE against the committed 'completed'
    state under READ COMMITTED and skips it. Only rows it actually flipped are
    superseded, so a just-completed pass keeps its member-visible proposals."""
    cancelled_ids = (
        session.execute(
            update(CoherenceJob)
            .where(
                CoherenceJob.workspace_id == workspace_id,
                CoherenceJob.status.in_(("queued", "running")),
            )
            .values(status="failed", error="workspace deleted", completed_at=datetime.now(UTC))
            .returning(CoherenceJob.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    for job_id in cancelled_ids:
        supersede_own_coherence(session, workspace_id=workspace_id, job_id=job_id)
    session.flush()
    return len(cancelled_ids)


def _supersede(session: Session, *, workspace_id: uuid.UUID, where_job, equals: bool) -> int:
    # equals=True (own/failure): origin_job_id == this job. equals=False (prior/success):
    # IS DISTINCT FROM, which also supersedes any NULL-origin_job_id coherence rows
    # (a plain `!=` would skip NULLs — spec §2.4).
    cond = (
        ProposedChange.origin_job_id == where_job
        if equals
        else ProposedChange.origin_job_id.is_distinct_from(where_job)
    )
    result = session.execute(
        update(ProposedChange)
        .where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.origin_kind == "coherence_session",
            ProposedChange.status == "pending",
            cond,
        )
        .values(status="superseded")
    )
    session.flush()
    return result.rowcount or 0


def supersede_prior_coherence(
    session: Session, *, workspace_id: uuid.UUID, job_id: uuid.UUID
) -> int:
    """On success: supersede prior passes' pending rows (origin_job_id != job_id)."""
    return _supersede(session, workspace_id=workspace_id, where_job=job_id, equals=False)


def supersede_own_coherence(session: Session, *, workspace_id: uuid.UUID, job_id: uuid.UUID) -> int:
    """On failure: supersede THIS job's partial pending rows (origin_job_id == job_id)."""
    return _supersede(session, workspace_id=workspace_id, where_job=job_id, equals=True)


def coherence_cap_exceeded(
    session: Session, *, workspace_id: uuid.UUID, settings: Settings
) -> bool:
    """True if today's (Stockholm-day) coherence in+out tokens are at/over the cap."""
    day_start = stockholm_day_start_utc()
    total = session.scalar(
        select(
            func.coalesce(func.sum(CoherenceJob.input_tokens + CoherenceJob.output_tokens), 0)
        ).where(
            CoherenceJob.workspace_id == workspace_id,
            CoherenceJob.queued_at >= day_start,
        )
    )
    return int(total or 0) >= settings.coherence_daily_token_cap


def reap_stuck_coherence_jobs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running job older than the TTL and supersede
    its partial rows, unwedging the workspace's active-unique slot. Pure DB."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(CoherenceJob)
        .where(CoherenceJob.status == "running", CoherenceJob.started_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in rows:
        job.status = "failed"
        job.error = "timed out"
        job.completed_at = datetime.now(UTC)
        supersede_own_coherence(session, workspace_id=job.workspace_id, job_id=job.id)
    session.flush()
    return len(rows)


def latest_coherence_job(session: Session, *, workspace_id: uuid.UUID) -> CoherenceJob | None:
    return session.scalar(
        select(CoherenceJob)
        .where(CoherenceJob.workspace_id == workspace_id)
        .order_by(CoherenceJob.queued_at.desc())
        .limit(1)
    )
