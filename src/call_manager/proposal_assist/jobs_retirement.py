"""The "jobs" retirement hook: terminalize a deleted workspace's job rows.

"Terminalize" means every NON-TERMINAL state, not just the two the worker is
actively driving — see _CANCELLABLE. A deleted workspace must leave no setup job
that can still be waited on, including one parked in 'waiting_for_rubric_review'
awaiting an owner who can no longer reach the workspace.

A neutral home so main.py and scheduler.py can both reach it without importing
the Phase-0 worker. This slice handles setup jobs; workspace-retirement Slice C
extends BOTH the hook and reconcile_cancelled_jobs to claim-analysis.

Why after the delete commits rather than inside its transaction: a worker's
heartbeat UPDATE holds its job row for the whole per-job transaction, so a
cancel inside the delete transaction would block on that row while holding the
uncommitted workspace deletion — stalling DELETE and hiding the deletion from
the worker's own liveness re-read.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import ClaimAnalysisJob, Workspace, WorkspaceSetupJob

logger = logging.getLogger(__name__)

# The hook's ceiling. Enforced with asyncio.wait_for in build_jobs_retirement_hook —
# a constant on its own bounds nothing, least of all a to_thread call. Every query
# below uses SKIP LOCKED, so this is a timeout for pathology, not for waiting.
_JOBS_CANCEL_SECONDS = 1.0

# Every NON-TERMINAL setup-job state. All three must be cancellable when the
# workspace is deleted, because none of them ever reaches a terminal state on
# its own once the workspace is gone:
#   * 'queued'   — claim_next_setup_job filters deleted workspaces out in SQL, so
#                  it would never be claimed again.
#   * 'running'  — the worker's own liveness checks fail it, but only if a worker
#                  is actually alive to notice.
#   * 'waiting_for_rubric_review' — parked awaiting a HUMAN action on a workspace
#                  that no longer exists. Nothing else in the system moves this
#                  row: it has no completed_at, requeue_stuck_setup_jobs
#                  deliberately never reaps it, and no worker will re-touch it.
#                  Left out of this tuple it stayed live forever.
# NB: _fail's already-terminal short-circuit in phases/setup.py also names
# 'waiting_for_rubric_review', for the unrelated reason that a job which already
# yielded to review must not be resurrected by a late failure. Different
# question, deliberately not the same list.
_CANCELLABLE = ("queued", "running", "waiting_for_rubric_review")

# Non-terminal claim-analysis states. Terminalizes to 'failed', already allowed by
# ck_claim_analysis_jobs_status — Slice C1 ships NO migration.
_CLAIM_ANALYSIS_CANCELLABLE = ("queued", "running")


def cancel_claim_analysis_jobs_for_workspace(
    session: Session, workspace_id: uuid.UUID, generation: int
) -> int:
    """Terminalize this workspace's active claim-analysis jobs. Caller commits.

    SKIP LOCKED for the same reason as the setup-job cancel: touch_running_job
    holds the job row for the worker's WHOLE per-job transaction, so waiting would
    make this hook inherit that lifetime and stall DELETE. Skipped rows converge
    via reconcile_cancelled_jobs.
    """
    ids = session.scalars(
        select(ClaimAnalysisJob.id)
        .where(
            ClaimAnalysisJob.workspace_id == workspace_id,
            ClaimAnalysisJob.status.in_(_CLAIM_ANALYSIS_CANCELLABLE),
            # The linearization point. This runs on an ORPHANED thread whenever the
            # hook's wait_for timed out (that bound cancels the await, not the
            # thread), and the thread escapes the coordinator's lifecycle lock the
            # same way — so "was the workspace still at my generation when I
            # started" is not a question anyone can answer outside the statement.
            # Ask it here, atomically with the write.
            exists().where(
                Workspace.id == workspace_id,
                Workspace.lifecycle_generation == generation,
            ),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if not ids:
        return 0
    session.execute(
        update(ClaimAnalysisJob)
        .where(ClaimAnalysisJob.id.in_(ids))
        .values(
            status="failed",
            last_error="cancelled: workspace deleted",
            updated_at=datetime.now(UTC),
        )
    )
    return len(ids)


def cancel_setup_jobs_for_workspace(
    session: Session, workspace_id: uuid.UUID, generation: int
) -> int:
    """Mark this workspace's active setup jobs 'cancelled'. Caller commits.

    SKIP LOCKED: a row a live worker holds is SKIPPED, never waited on, so this
    can never inherit that worker's transaction lifetime. Skipped rows converge
    via reconcile_cancelled_jobs.
    """
    ids = session.scalars(
        select(WorkspaceSetupJob.id)
        .where(
            WorkspaceSetupJob.workspace_id == workspace_id,
            WorkspaceSetupJob.status.in_(_CANCELLABLE),
            # The linearization point. This runs on an ORPHANED thread whenever the
            # hook's wait_for timed out (that bound cancels the await, not the
            # thread), and the thread escapes the coordinator's lifecycle lock the
            # same way — so "was the workspace still at my generation when I
            # started" is not a question anyone can answer outside the statement.
            # Ask it here, atomically with the write.
            exists().where(
                Workspace.id == workspace_id,
                Workspace.lifecycle_generation == generation,
            ),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if not ids:
        return 0
    session.execute(
        update(WorkspaceSetupJob)
        .where(WorkspaceSetupJob.id.in_(ids))
        .values(
            status="cancelled",
            completed_at=datetime.now(UTC),
            last_error="cancelled: workspace deleted",
        )
    )
    return len(ids)


def build_jobs_retirement_hook(
    session_factory: sessionmaker,
) -> Callable[[uuid.UUID, int], Awaitable[int]]:
    """Build the coordinator hook. The coordinator is async and these are sync DB
    writes, so the work runs in a thread — bounded by wait_for.

    *generation* is threaded into the SQL predicate so the hook is
    generation-conditional in SQL: an orphaned thread (one where the wait_for
    timed out, cancelling the await but not the thread) that resumes after a later
    transition has moved the generation will update ZERO rows.
    """

    async def _hook(workspace_id: uuid.UUID, generation: int) -> int:
        def _work() -> int:
            with session_factory() as session:
                n = cancel_setup_jobs_for_workspace(session, workspace_id, generation)
                n += cancel_claim_analysis_jobs_for_workspace(
                    session, workspace_id, generation
                )
                session.commit()
                return n

        return await asyncio.wait_for(
            asyncio.to_thread(_work), timeout=_JOBS_CANCEL_SECONDS
        )

    return _hook


def reconcile_cancelled_jobs(session: Session) -> int:
    """Terminalize active job rows belonging to soft-deleted workspaces.

    Convergence for rows the hook SKIP LOCKED past. Deliberately owns no LLM
    client and is scheduled independently of the Phase-0 tick, which returns
    early when DRAFTING_ENABLED is false or ANTHROPIC_API_KEY is unset — hiding
    reconciliation behind that path would strand every skipped cancellation on a
    deployment with drafting off.

    Slice C extends this to ClaimAnalysisJob (terminalizing to 'failed', which
    that table's CheckConstraint already allows).

    Returns rows touched. Caller commits.
    """
    dead_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_not(None))
    total = 0

    setup_ids = session.scalars(
        select(WorkspaceSetupJob.id)
        .where(
            WorkspaceSetupJob.status.in_(_CANCELLABLE),
            WorkspaceSetupJob.workspace_id.in_(dead_ws),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if setup_ids:
        session.execute(
            update(WorkspaceSetupJob)
            .where(WorkspaceSetupJob.id.in_(setup_ids))
            .values(
                status="cancelled",
                completed_at=datetime.now(UTC),
                last_error="cancelled: workspace deleted (reconciled)",
            )
        )
        total += len(setup_ids)

    # NOT an early return above: a workspace with no setup jobs at all must still
    # have its claim-analysis rows reconciled.
    claim_ids = session.scalars(
        select(ClaimAnalysisJob.id)
        .where(
            ClaimAnalysisJob.status.in_(_CLAIM_ANALYSIS_CANCELLABLE),
            ClaimAnalysisJob.workspace_id.in_(dead_ws),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if claim_ids:
        session.execute(
            update(ClaimAnalysisJob)
            .where(ClaimAnalysisJob.id.in_(claim_ids))
            .values(
                status="failed",
                last_error="cancelled: workspace deleted (reconciled)",
                updated_at=datetime.now(UTC),
            )
        )
        total += len(claim_ids)

    if total:
        logger.info(
            "jobs_retirement.reconciled",
            extra={"event": "jobs_retirement_reconciled", "rows": total},
        )
    return total
