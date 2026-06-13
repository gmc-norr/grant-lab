"""Orphan sweep — transition stale `turn_jobs.status='running'` rows
for a workspace to 'failed' with error='orphaned'.

Called from WorkspaceController.launch() before handing back a fresh
handle. Cleans up after a crashed prior subprocess or a redeploy
that killed an in-flight turn."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome


def sweep_orphaned_turn_jobs(session: Session, workspace_id: uuid.UUID) -> int:
    """Mark any 'running' turn_jobs for this workspace 'failed' with
    error='orphaned', writing a viewer-safe system row + audit event per job.
    Returns the count of rows actually transitioned. Caller commits.
    Workspace-scoped — does not touch other workspaces.
    """
    rows = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "running",
        )
    ).all()
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
