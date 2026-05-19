"""Workspace lifecycle service functions (base spec).

Single entrypoint today: `create_workspace`. It does two things in one
unit of work:

  1. Insert a Workspace row.
  2. Write a workspace_events(kind='created') audit row.

The caller commits. If either fails, the caller rolls back and no partial
state lands.

This module deliberately does NOT enqueue a Phase 0 setup job —
`workspace_setup_jobs` is owned by Slice A Part 2's plan, which will add
its own `enqueue_phase0(workspace_id)` helper or extend `create_workspace`
once its migration lands. Keeping that coupling out of B1 lets B1 ship
without dragging Slice A Part 2's tables into the base-spec scope.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from call_monitor.db.models import Workspace, WorkspaceEvent


def create_workspace(
    *,
    session: Session,
    owner_id: int,
    title: str,
    call_id: int | None = None,
    profile_id: int | None = None,
) -> Workspace:
    """Create a new workspace and write the 'created' audit event.
    Returns the Workspace (with `.id` populated via session.flush())."""
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
    return ws
