"""The "jobs" retirement hook: terminalize a deleted workspace's job rows.

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

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import WorkspaceSetupJob

logger = logging.getLogger(__name__)

# The hook's ceiling. Enforced with asyncio.wait_for in build_jobs_retirement_hook —
# a constant on its own bounds nothing, least of all a to_thread call. Every query
# below uses SKIP LOCKED, so this is a timeout for pathology, not for waiting.
_JOBS_CANCEL_SECONDS = 1.0

_CANCELLABLE = ("queued", "running")


def cancel_setup_jobs_for_workspace(session: Session, workspace_id: uuid.UUID) -> int:
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
) -> Callable[[uuid.UUID], Awaitable[int]]:
    """Build the coordinator hook. The coordinator is async and these are sync DB
    writes, so the work runs in a thread — bounded by wait_for."""

    async def _hook(workspace_id: uuid.UUID) -> int:
        def _work() -> int:
            with session_factory() as session:
                n = cancel_setup_jobs_for_workspace(session, workspace_id)
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
    from call_manager.db.models import Workspace

    dead_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_not(None))
    ids = session.scalars(
        select(WorkspaceSetupJob.id)
        .where(
            WorkspaceSetupJob.status.in_(_CANCELLABLE),
            WorkspaceSetupJob.workspace_id.in_(dead_ws),
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
            last_error="cancelled: workspace deleted (reconciled)",
        )
    )
    logger.info(
        "jobs_retirement.reconciled",
        extra={"event": "jobs_retirement_reconciled", "rows": len(ids)},
    )
    return len(ids)
