"""Orphan sweep — transition stale `turn_jobs.status='running'` rows
for a workspace to 'failed' with error='orphaned'.

Called from WorkspaceController.launch() before handing back a fresh
handle. Cleans up after a crashed prior subprocess or a redeploy
that killed an in-flight turn."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from call_monitor.db.models import TurnJob


def sweep_orphaned_turn_jobs(session: Session, workspace_id: uuid.UUID) -> int:
    """Mark any 'running' turn_jobs for this workspace as 'failed' with
    error='orphaned'. Returns the count of rows swept.

    Caller commits. Workspace-scoped — does not touch other workspaces
    even if they also have stale 'running' rows."""
    result = session.execute(
        update(TurnJob)
        .where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "running",
        )
        .values(
            status="failed",
            error="orphaned",
            completed_at=datetime.now(UTC),
        )
    )
    return result.rowcount or 0
