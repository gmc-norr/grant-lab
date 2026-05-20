"""Workspace lifecycle service functions.

Single entrypoint today: `create_workspace`. It does three things in one
unit of work:

  1. Insert a Workspace row.
  2. Write a workspace_events(kind='created') audit row.
  3. Enqueue a workspace_setup_jobs row so the Phase 0 worker
     (src/call_manager/proposal_assist/phases/setup.py) snapshots the
     call body, extracts the section + rubric templates, and yields at
     waiting_for_rubric_review for the workspace owner to confirm.

The caller commits. If any of the three fails, the caller rolls back and
no partial state lands.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from call_manager.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob


def create_workspace(
    *,
    session: Session,
    owner_id: int,
    title: str,
    call_id: int | None = None,
    profile_id: int | None = None,
) -> Workspace:
    """Create a new workspace, write the 'created' audit event, and enqueue
    the Phase 0 setup job. Returns the Workspace (with `.id` populated via
    session.flush())."""
    ws = Workspace(
        owner_id=owner_id,
        title=title,
        call_id=call_id,
        profile_id=profile_id,
    )
    session.add(ws)
    session.flush()  # populates ws.id from the DB default

    session.add(
        WorkspaceEvent(
            workspace_id=ws.id,
            kind="created",
            payload={
                "title": title,
                "call_id": call_id,
                "profile_id": profile_id,
            },
            actor_id=owner_id,
        )
    )
    session.add(
        WorkspaceSetupJob(
            workspace_id=ws.id,
            status="queued",
        )
    )
    return ws
