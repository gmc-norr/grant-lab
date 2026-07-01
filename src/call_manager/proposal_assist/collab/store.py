"""Sync persistence for the Yjs update log (the room's durable truth).

These run on a worker thread (the WS handler offloads them via
run_in_threadpool), so they take an ordinary sync Session.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import YjsUpdate


def append_update(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    actor_id: int | None,
    update: bytes,
) -> None:
    """Append one Yjs update for a room, tagged with the sending user."""
    session.add(
        YjsUpdate(
            workspace_id=workspace_id,
            artifact=artifact,
            actor_id=actor_id,
            update_bytes=update,
        )
    )
    session.commit()


def load_updates(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> list[bytes]:
    """All persisted updates for a room, in insertion order (for replay)."""
    rows = (
        session.execute(
            select(YjsUpdate.update_bytes)
            .where(YjsUpdate.workspace_id == workspace_id, YjsUpdate.artifact == artifact)
            .order_by(YjsUpdate.id)
        )
        .scalars()
        .all()
    )
    return list(rows)
