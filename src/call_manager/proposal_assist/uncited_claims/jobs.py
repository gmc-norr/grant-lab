"""Pure-DB helpers for the uncited_claims_jobs queue. The caller owns the transaction
(these flush, never commit), mirroring evaluator/jobs.py. The final claims list is a
single JSONB on the job row, so 'latest completed wins' is a read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.turns.caps import _stockholm_day_start_utc


def enqueue_uncited_claims_run(
    session: Session, *, workspace_id: uuid.UUID, requested_by: int | None
) -> UncitedClaimsJob:
    """Create a queued job, or return the existing active (queued|running) one.
    Race-safe against uq_uncited_claims_jobs_active via a SAVEPOINT."""
    existing = session.scalar(
        select(UncitedClaimsJob).where(
            UncitedClaimsJob.workspace_id == workspace_id,
            UncitedClaimsJob.status.in_(("queued", "running")),
        )
    )
    if existing is not None:
        return existing
    job = UncitedClaimsJob(workspace_id=workspace_id, status="queued", requested_by=requested_by)
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        if job in session:
            session.expunge(job)
        winner = session.scalar(
            select(UncitedClaimsJob).where(
                UncitedClaimsJob.workspace_id == workspace_id,
                UncitedClaimsJob.status.in_(("queued", "running")),
            )
        )
        if winner is None:
            raise RuntimeError(
                "uncited-claims enqueue lost the active-unique race but found no active job"
            ) from None
        return winner
    return job


def claim_next_uncited_claims_job(session: Session) -> UncitedClaimsJob | None:
    """Claim the oldest queued job whose workspace is LIVE and has no running job.
    FOR UPDATE SKIP LOCKED. Soft-deleted workspaces excluded (no post-delete spend)."""
    running_ws = select(UncitedClaimsJob.workspace_id).where(UncitedClaimsJob.status == "running")
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    job = session.scalar(
        select(UncitedClaimsJob)
        .where(
            UncitedClaimsJob.status == "queued",
            UncitedClaimsJob.workspace_id.not_in(running_ws),
            UncitedClaimsJob.workspace_id.in_(live_ws),
        )
        .order_by(UncitedClaimsJob.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    session.flush()
    return job


def cancel_uncited_claims_jobs_for_workspace(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Fail any active (queued|running) job for a workspace (soft-delete handler). ONE
    atomic UPDATE ... RETURNING — never an ORM load-then-write (which could clobber a job
    the success finalizer is concurrently completing). Mirrors the evaluator helper."""
    cancelled_ids = (
        session.execute(
            update(UncitedClaimsJob)
            .where(
                UncitedClaimsJob.workspace_id == workspace_id,
                UncitedClaimsJob.status.in_(("queued", "running")),
            )
            .values(status="failed", error="workspace deleted", completed_at=datetime.now(UTC))
            .returning(UncitedClaimsJob.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    session.flush()
    return len(cancelled_ids)


def uncited_claims_cap_exceeded(
    session: Session, *, workspace_id: uuid.UUID, settings: Settings
) -> bool:
    """True if today's (Stockholm-day) in+out tokens are at/over the cap."""
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(
            func.coalesce(
                func.sum(UncitedClaimsJob.input_tokens + UncitedClaimsJob.output_tokens), 0
            )
        ).where(
            UncitedClaimsJob.workspace_id == workspace_id,
            UncitedClaimsJob.queued_at >= day_start,
        )
    )
    return int(total or 0) >= settings.uncited_claims_daily_token_cap


def reap_stuck_uncited_claims_jobs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running job older than the TTL. FOR UPDATE SKIP LOCKED."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(UncitedClaimsJob)
        .where(UncitedClaimsJob.status == "running", UncitedClaimsJob.started_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in rows:
        job.status = "failed"
        job.error = "timed out"
        job.completed_at = datetime.now(UTC)
    session.flush()
    return len(rows)


def latest_uncited_claims_job(
    session: Session, *, workspace_id: uuid.UUID
) -> UncitedClaimsJob | None:
    return session.scalar(
        select(UncitedClaimsJob)
        .where(UncitedClaimsJob.workspace_id == workspace_id)
        .order_by(UncitedClaimsJob.queued_at.desc())
        .limit(1)
    )


def latest_completed_uncited_claims_job(
    session: Session, *, workspace_id: uuid.UUID
) -> UncitedClaimsJob | None:
    return session.scalar(
        select(UncitedClaimsJob)
        .where(
            UncitedClaimsJob.workspace_id == workspace_id,
            UncitedClaimsJob.status == "completed",
        )
        .order_by(UncitedClaimsJob.queued_at.desc())
        .limit(1)
    )


def dismissed_claim_hashes(session: Session, *, workspace_id: uuid.UUID) -> set[str]:
    """Distinct claim_hash values dismissed in this workspace. Reads ONLY the feature's
    own kind 'uncited_claim_dismissed' — NEVER the annotation 'user_claim_dismissed'."""
    rows = session.scalars(
        select(WorkspaceEvent.payload["claim_hash"].astext).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.kind == "uncited_claim_dismissed",
        )
    ).all()
    return {h for h in rows if h}
