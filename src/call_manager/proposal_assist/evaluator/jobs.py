"""Pure-DB helpers for the evaluator_jobs queue. The caller owns the transaction
(these flush, never commit), mirroring coherence/jobs.py. No supersede logic — the
scorecard is a single JSONB on the job row, so 'latest completed wins' is a read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.turns.caps import _stockholm_day_start_utc


def enqueue_evaluator_run(
    session: Session, *, workspace_id: uuid.UUID, requested_by: int | None
) -> EvaluatorJob:
    """Create a queued job, or return the existing active (queued|running) one.
    Race-safe against uq_evaluator_jobs_active: the INSERT runs inside a SAVEPOINT
    so a unique-violation rolls back only the savepoint, leaving the outer txn
    usable for the re-read."""
    existing = session.scalar(
        select(EvaluatorJob).where(
            EvaluatorJob.workspace_id == workspace_id,
            EvaluatorJob.status.in_(("queued", "running")),
        )
    )
    if existing is not None:
        return existing
    job = EvaluatorJob(workspace_id=workspace_id, status="queued", requested_by=requested_by)
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        if job in session:
            session.expunge(job)
        winner = session.scalar(
            select(EvaluatorJob).where(
                EvaluatorJob.workspace_id == workspace_id,
                EvaluatorJob.status.in_(("queued", "running")),
            )
        )
        if winner is None:
            raise RuntimeError(
                "evaluator enqueue lost the active-unique race but found no active job"
            ) from None
        return winner
    return job


def claim_next_evaluator_job(session: Session) -> EvaluatorJob | None:
    """Claim the oldest queued job whose workspace is LIVE and has no running job.
    FOR UPDATE SKIP LOCKED so parallel ticks don't fight. Soft-deleted workspaces
    are excluded so a pass can't burn tokens after deletion.

    The join captures lifecycle_generation IN THE SAME STATEMENT as the FOR UPDATE
    lock (Global Constraint 2). A separate SELECT would reopen the delete/restore
    race the fence exists to close.

    `of=EvaluatorJob` keeps the lock on the job row only. Without `of=`, PostgreSQL
    also locks the joined Workspace row, putting this claim in the delete path's way
    and inverting the lock order the architecture spec fixes in §5."""
    running_ws = select(EvaluatorJob.workspace_id).where(EvaluatorJob.status == "running")
    row = session.execute(
        select(EvaluatorJob, Workspace.lifecycle_generation)
        .join(Workspace, Workspace.id == EvaluatorJob.workspace_id)
        .where(
            EvaluatorJob.status == "queued",
            EvaluatorJob.workspace_id.not_in(running_ws),
            Workspace.soft_deleted_at.is_(None),
        )
        .order_by(EvaluatorJob.queued_at)
        .limit(1)
        # `of=` keeps FOR UPDATE on the JOB only. Without it PostgreSQL would
        # also lock the Workspace row, putting this claim in the delete path's
        # way and inverting the lock order the architecture spec fixes in §5.
        .with_for_update(of=EvaluatorJob, skip_locked=True)
    ).first()
    if row is None:
        return None
    job, generation = row
    job.status = "running"
    job.started_at = datetime.now(UTC)
    job.claimed_at_generation = generation
    session.flush()
    return job


def cancel_evaluator_jobs_for_workspace(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Fail any active (queued|running) evaluator job for a workspace (soft-delete
    handler) so a queued/running pass can't spend tokens after deletion. ONE atomic
    UPDATE ... WHERE status IN (...) RETURNING — never an ORM load-then-write, which
    could clobber a job the success finalizer is concurrently completing."""
    cancelled_ids = (
        session.execute(
            update(EvaluatorJob)
            .where(
                EvaluatorJob.workspace_id == workspace_id,
                EvaluatorJob.status.in_(("queued", "running")),
            )
            .values(status="failed", error="workspace deleted", completed_at=datetime.now(UTC))
            .returning(EvaluatorJob.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    session.flush()
    return len(cancelled_ids)


def evaluator_cap_exceeded(
    session: Session, *, workspace_id: uuid.UUID, settings: Settings
) -> bool:
    """True if today's (Stockholm-day) evaluator in+out tokens are at/over the cap."""
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(
            func.coalesce(func.sum(EvaluatorJob.input_tokens + EvaluatorJob.output_tokens), 0)
        ).where(
            EvaluatorJob.workspace_id == workspace_id,
            EvaluatorJob.queued_at >= day_start,
        )
    )
    return int(total or 0) >= settings.evaluator_daily_token_cap


def reap_stuck_evaluator_jobs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running job older than the TTL, unwedging the
    workspace's active-unique slot. Pure DB. FOR UPDATE SKIP LOCKED."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(EvaluatorJob)
        .where(EvaluatorJob.status == "running", EvaluatorJob.started_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in rows:
        job.status = "failed"
        job.error = "timed out"
        job.completed_at = datetime.now(UTC)
    session.flush()
    return len(rows)


def latest_evaluator_job(session: Session, *, workspace_id: uuid.UUID) -> EvaluatorJob | None:
    return session.scalar(
        select(EvaluatorJob)
        .where(EvaluatorJob.workspace_id == workspace_id)
        .order_by(EvaluatorJob.queued_at.desc())
        .limit(1)
    )


def latest_completed_evaluator_job(
    session: Session, *, workspace_id: uuid.UUID
) -> EvaluatorJob | None:
    return session.scalar(
        select(EvaluatorJob)
        .where(
            EvaluatorJob.workspace_id == workspace_id,
            EvaluatorJob.status == "completed",
        )
        .order_by(EvaluatorJob.queued_at.desc())
        .limit(1)
    )
