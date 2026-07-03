"""Sync persistence for the Yjs update log (the room's durable truth).

These run on a worker thread (the WS handler offloads them via
run_in_threadpool), so they take an ordinary sync Session.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
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


def compact_snapshot(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    min_id: int,
    merged_update: bytes,
    delete_ids: list[int],
) -> None:
    """Squash a checkpointed prefix of the log into ONE full-state row WITHOUT
    changing replay order. The merged full-state update OVERWRITES the row with
    the LOWEST id in the snapshot (``min_id``), so it keeps an id below every tail
    row (tail ids are all > max_id >= min_id) and ``load_updates`` (ORDER BY id)
    always replays merged-before-tail. pycrdt does NOT converge tail-before-merged,
    so this ordering is load-bearing (Slice 3b-1). The other merged rows are
    deleted by EXPLICIT id (never a blanket ``id <= max_id``), so a row that
    commits after the snapshot is never clobbered. Stages on the caller's txn."""
    session.execute(
        sa_update(YjsUpdate)
        .where(
            YjsUpdate.workspace_id == workspace_id,
            YjsUpdate.artifact == artifact,
            YjsUpdate.id == min_id,
        )
        .values(update_bytes=merged_update)
    )
    if delete_ids:
        session.execute(
            sa_delete(YjsUpdate).where(
                YjsUpdate.workspace_id == workspace_id,
                YjsUpdate.artifact == artifact,
                YjsUpdate.id.in_(delete_ids),
            )
        )
    session.flush()
