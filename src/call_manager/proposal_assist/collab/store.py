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
    commit: bool = True,
) -> None:
    """Append one Yjs update for a room, tagged with the sending user.

    ``commit=True`` (default) commits immediately — the Slice-1 ``room.handle``
    per-update persistence contract. ``commit=False`` stages the row on the
    caller's transaction (the write kernel commits the update + checkpoint +
    accept status flip together, atomically)."""
    session.add(
        YjsUpdate(
            workspace_id=workspace_id,
            artifact=artifact,
            actor_id=actor_id,
            update_bytes=update,
        )
    )
    if commit:
        session.commit()
    else:
        session.flush()


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


def load_rows(
    session: Session, *, workspace_id: uuid.UUID, artifact: str
) -> list[tuple[int, int | None, bytes]]:
    """One ordered snapshot of the room's log as (id, actor_id, update_bytes).

    The checkpoint path builds the doc, computes max_id, and derives authors
    from this single read so they are mutually consistent (a concurrent
    append landing after this SELECT is simply excluded from the snapshot).
    """
    rows = session.execute(
        select(YjsUpdate.id, YjsUpdate.actor_id, YjsUpdate.update_bytes)
        .where(YjsUpdate.workspace_id == workspace_id, YjsUpdate.artifact == artifact)
        .order_by(YjsUpdate.id)
    ).all()
    return [(int(r[0]), (int(r[1]) if r[1] is not None else None), r[2]) for r in rows]
