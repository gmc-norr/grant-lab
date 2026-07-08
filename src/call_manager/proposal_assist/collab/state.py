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


# 3b-5d.D4 severance recheck (Item 2b): a git-first writer's PRE-mutex severance
# guard (in the route) can race enable-on-touch — the flip may commit while the
# writer is blocked on the commit mutex. So every git-first writer re-checks the
# flag UNDER the mutex via raise_if_collab_severed and aborts writing NOTHING.
# The detail is byte-identical to the routes' pre-mutex 409 so the client behaves
# uniformly (reload -> mount the live editor). No-op when the flag is off /
# kill-switch armed (is_collab_enabled -> False), so flag-off paths are unchanged.
COLLAB_SEVERED_DETAIL = "this workspace now uses the live editor — reload the page"


class CollabSeveranceConflict(RuntimeError):
    """Raised UNDER the commit mutex when the artifact flipped to collab after a
    git-first writer's pre-mutex severance guard passed (enable-on-touch raced
    the in-flight write). The writer aborts, writing NOTHING; routes map this to
    the same 409 as the pre-mutex severance guard."""


def raise_if_collab_severed(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> None:
    """Abort a git-first write if the artifact is (now) collab-enabled. Called by
    every git-first writer while it holds the commit mutex. No-op when the flag is
    off / kill-switch armed, so flag-off paths stay byte-for-byte."""
    if is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        raise CollabSeveranceConflict(str(workspace_id))


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
