"""Per-(workspace, artifact) collab flag + checkpoint watermark accessors (sync)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from call_manager.db.models import CollabArtifactState


def _get(session: Session, workspace_id: uuid.UUID, artifact: str) -> CollabArtifactState | None:
    return session.get(CollabArtifactState, (workspace_id, artifact))


def is_collab_enabled(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> bool:
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
    return row.last_checkpoint_update_id if row and row.last_checkpoint_update_id else 0


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
