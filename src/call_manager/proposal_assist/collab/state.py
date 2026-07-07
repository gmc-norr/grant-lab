"""Per-(workspace, artifact) collab flag + checkpoint watermark accessors (sync)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from call_manager.db.models import CollabArtifactState

# 3b-5d.D4 kill-switch: a process-wide override consulted by is_collab_enabled.
# Armed once at app creation from Settings.collab_kill_switch (create_app), and
# by tests via set_kill_switch. Module-level ON PURPOSE (not threaded through
# call sites): every gate in the tree already consults is_collab_enabled, and
# the switch is an emergency, restart-scoped toggle — not per-request state.
_KILL_SWITCH = False


def set_kill_switch(value: bool) -> None:
    global _KILL_SWITCH
    _KILL_SWITCH = bool(value)


def kill_switch_engaged() -> bool:
    return _KILL_SWITCH


def _get(session: Session, workspace_id: uuid.UUID, artifact: str) -> CollabArtifactState | None:
    return session.get(CollabArtifactState, (workspace_id, artifact))


def is_collab_enabled(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> bool:
    if _KILL_SWITCH:
        return False
    row = _get(session, workspace_id, artifact)
    return bool(row and row.enabled_at is not None)


def enable_collab(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> None:
    row = _get(session, workspace_id, artifact)
    if row is None:
        row = CollabArtifactState(workspace_id=workspace_id, artifact=artifact)
        session.add(row)
    if row.enabled_at is None:  # sticky: don't overwrite an existing enable time
        row.enabled_at = datetime.now(UTC)


def get_checkpoint_watermark(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> int:
    row = _get(session, workspace_id, artifact)
    return (
        row.last_checkpoint_update_id
        if row and row.last_checkpoint_update_id is not None
        else 0
    )


def advance_checkpoint(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    up_to_update_id: int,
    commit_sha: str,
) -> None:
    row = _get(session, workspace_id, artifact)
    if row is None:
        row = CollabArtifactState(workspace_id=workspace_id, artifact=artifact)
        session.add(row)
    row.last_checkpoint_update_id = up_to_update_id
    row.last_checkpoint_commit_sha = commit_sha


def reset_checkpoint(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> None:
    """Kill-switch handover ONLY: return the artifact to the 'never opened'
    state (the log is truncated separately, same transaction) so a
    post-kill-switch reconnect hydrates fresh from git HEAD. enabled_at is
    deliberately untouched (sticky-forward)."""
    row = _get(session, workspace_id, artifact)
    if row is not None:
        row.last_checkpoint_update_id = None
        row.last_checkpoint_commit_sha = None
