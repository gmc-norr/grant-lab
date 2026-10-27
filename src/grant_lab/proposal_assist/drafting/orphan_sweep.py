"""Orphan sweep — transition stale `turn_jobs.status='running'` rows
for a workspace to 'failed' with error='orphaned'.

Called from WorkspaceController.launch() before handing back a fresh
handle. Cleans up after a crashed prior subprocess or a redeploy
that killed an in-flight turn."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from grant_lab.db.models import TurnJob
from grant_lab.proposal_assist.turns.outcomes import record_terminal_outcome


def sweep_orphaned_turn_jobs(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    protect_turn_job_id: uuid.UUID | None = None,
) -> int:
    """Mark any 'running' turn_jobs for this workspace 'failed' with
    error='orphaned', writing a viewer-safe system row + audit event per job.
    Returns the count of rows actually transitioned. Caller commits.
    Workspace-scoped — does not touch other workspaces.

    `protect_turn_job_id` exempts one row. The sweep's premise is that a fresh
    subprocess means every 'running' row is a leftover, which holds for a cold
    launch — but the lost-SDK-session recovery (M-PA-4) relaunches DURING its
    own turn, and that row is live, not stale. Sweeping it would flip the turn
    to failed and show the user an "interrupted" notice mid-recovery. Default
    None keeps the cold-launch behaviour exactly as it was.
    """
    rows = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "running",
        )
    ).all()
    if protect_turn_job_id is not None:
        rows = [row for row in rows if row.id != protect_turn_job_id]
    swept = 0
    for row in rows:
        if record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="failed",
            expected_status="running",
            raw_error="orphaned",
            emit_chat=True,
        ):
            swept += 1
    return swept
