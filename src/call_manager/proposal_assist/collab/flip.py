"""Enable-on-touch flip seam (3b-5d.D4): the ONLY prod caller of enable_collab.

maybe_auto_enable is invoked from the draft/spec read models — the first read
of an artifact (kill-switch off) enables collab for it, stickily and
per-artifact. New workspaces converge on their first cockpit load; existing
ones on their next open. The D4 pipe-table hold-back was REMOVED by the tables
slice (docs/superpowers/specs/2026-07-08-collab-tables-design.md §G) once the
bridge gained lossless GFM table round-trip — previously held-back artifacts
now enable lazily on their next read."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    WorkspaceDeleted,
    acquire_commit_mutex_live,
)

logger = logging.getLogger(__name__)

# The flip is a read-path side effect and must not stall an interactive GET on a
# wedged writer. It acquires the commit mutex ONCE per artifact lifetime (the
# is_collab_enabled short-circuit fires forever after), so a bounded, modest
# timeout is safe: on contention we defer the flip and the NEXT read retries.
_FLIP_MUTEX_TIMEOUT_SECONDS = 5


def maybe_auto_enable(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
) -> bool:
    """Sticky lazy flip. Returns the artifact's EFFECTIVE enabled state.

    Enables iff: kill-switch off AND not already enabled AND the workspace is
    not soft-deleted (B-mutex: the live-mutex helper refuses a retired
    workspace under the lock). The enable is committed while HOLDING the
    workspace commit mutex — the linearization anchor the D4 severance-recheck
    family (writers' under-mutex re-reads) depends on; a route blocked on the
    mutex re-checks the flag after us and sees the enable. If the mutex can't
    be acquired (a writer holds it), we
    defer: return False and stay legacy — the next read retries. COMMITS the
    enable itself (read models otherwise never write; the session factory is
    expire_on_commit=False so callers' loaded rows survive). The concurrent
    first-read insert race resolves via IntegrityError → the other request
    won → report enabled."""
    if state.kill_switch_engaged():
        return False
    if state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return True
    try:
        acquire_commit_mutex_live(
            session, workspace_id, timeout_seconds=_FLIP_MUTEX_TIMEOUT_SECONDS
        )
    except CommitMutexTimeout:
        session.rollback()
        return False
    except WorkspaceDeleted:
        # Never enable collab on a deleted workspace — enable_collab writes a
        # CollabArtifactState row and COMMITS. Stay legacy; there is no next read.
        session.rollback()
        return False
    state.enable_collab(session, workspace_id=workspace_id, artifact=artifact)
    try:
        session.commit()  # releases the mutex
    except IntegrityError:
        session.rollback()  # concurrent first read won the insert — same outcome
    logger.info(
        "collab.flip_enabled",
        extra={
            "event": "collab_flip_enabled",
            "workspace_id": str(workspace_id),
            "artifact": artifact,
        },
    )
    return True
