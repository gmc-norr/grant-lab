"""Sync persistence for the Yjs update log (the room's durable truth).

These run on a worker thread (the WS handler offloads them via
run_in_threadpool), so they take an ordinary sync Session.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete as sa_delete
from sqlalchemy import exists, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from call_manager.db.models import CollabArtifactState, Workspace, YjsUpdate


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


def has_authored_updates(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> bool:
    """True if any live log row for (workspace_id, artifact) carries a non-None
    actor — i.e. the room has real edits beyond the unauthored hydration seed.

    The tight-vs-loose distinction (3b-5d.D4): a seed-only room serializes to
    the ProseMirror-canonical ("loose") form, while git HEAD and pre-flip
    annotation section_shas are the raw ("tight") bytes. Readers/writers that
    anchor by section_sha must use raw HEAD text until the first authored edit.
    """
    return bool(
        session.execute(
            select(
                exists().where(
                    YjsUpdate.workspace_id == workspace_id,
                    YjsUpdate.artifact == artifact,
                    YjsUpdate.actor_id.is_not(None),
                )
            )
        ).scalar()
    )


def find_checkpoint_candidates(session: Session, *, min_tail: int) -> list[tuple[uuid.UUID, str]]:
    """`(workspace_id, artifact)` pairs of LIVE (non-soft-deleted) workspaces whose
    log has at least `min_tail` rows above their checkpoint watermark.

    `YjsUpdate.id` is a GLOBAL autoincrement, so `max_id - watermark` would count
    other rooms' interleaved ids — the candidate predicate must COUNT rows whose id
    exceeds each room's own watermark (`HAVING count >= min_tail`). Soft-deleted
    workspaces are excluded (their tail must not be checkpointed — mirrors the
    eviction guard in permissions.make_checkpoint_on_evict)."""
    rows = session.execute(
        select(YjsUpdate.workspace_id, YjsUpdate.artifact)
        .join(Workspace, Workspace.id == YjsUpdate.workspace_id)
        .outerjoin(
            CollabArtifactState,
            (CollabArtifactState.workspace_id == YjsUpdate.workspace_id)
            & (CollabArtifactState.artifact == YjsUpdate.artifact),
        )
        .where(
            Workspace.soft_deleted_at.is_(None),
            YjsUpdate.id > func.coalesce(CollabArtifactState.last_checkpoint_update_id, 0),
        )
        .group_by(YjsUpdate.workspace_id, YjsUpdate.artifact)
        .having(func.count() >= min_tail)
    ).all()
    return [(r[0], r[1]) for r in rows]


def find_artifacts_with_updates(session: Session) -> list[tuple[uuid.UUID, str]]:
    """DISTINCT (workspace_id, artifact) owning ANY log rows, live workspaces
    only. The kill-switch handover needs every artifact with a log — including
    fully-checkpointed ones (a stale log would still clobber kill-window
    git-first writes on replay after the switch lifts), which is why this is
    NOT find_checkpoint_candidates(min_tail=0) (that only sees rows above the
    watermark)."""
    rows = session.execute(
        select(YjsUpdate.workspace_id, YjsUpdate.artifact)
        .join(Workspace, Workspace.id == YjsUpdate.workspace_id)
        .where(Workspace.soft_deleted_at.is_(None))
        .distinct()
    ).all()
    return [(r[0], r[1]) for r in rows]


def truncate_log(session: Session, *, workspace_id: uuid.UUID, artifact: str) -> int:
    """Delete ALL log rows for one artifact.

    Two callers, both of which MUST have checkpointed the full log to git in the
    SAME transaction: the kill-switch handover (handover.py) and a branch switch
    (branches.py §4.2 step 6). Both ALSO call state.reset_checkpoint in that
    transaction: a truncated log is a new document, and reset_checkpoint is
    what bumps `doc_epoch` so a client from before the truncate is refused.

    Returns the number of rows deleted."""
    res = session.execute(
        sa_delete(YjsUpdate).where(
            YjsUpdate.workspace_id == workspace_id, YjsUpdate.artifact == artifact
        )
    )
    return int(res.rowcount or 0)


def compact_snapshot(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    artifact: str,
    min_id: int,
    merged_update: bytes,
    merged_actor_id: int | None,
    delete_ids: list[int],
) -> None:
    """Squash a checkpointed prefix of the log into ONE full-state row WITHOUT
    changing replay order. The merged full-state update OVERWRITES the row with
    the LOWEST id in the snapshot (``min_id``), so it keeps an id below every tail
    row (tail ids are all > max_id >= min_id) and ``load_updates`` (ORDER BY id)
    always replays merged-before-tail. pycrdt does NOT converge tail-before-merged,
    so this ordering is load-bearing (Slice 3b-1). The other merged rows are
    deleted by EXPLICIT id (never a blanket ``id <= max_id``), so a row that
    commits after the snapshot is never clobbered. Stages on the caller's txn.
    The merged row also inherits ``merged_actor_id`` (the most-recent author in
    the snapshot) so ``load_rows`` still reflects the last author after compaction;
    because the merged row sits at/below the checkpoint watermark, this attribution
    is observable metadata only and never re-triggers checkpoint authorship."""
    session.execute(
        sa_update(YjsUpdate)
        .where(
            YjsUpdate.workspace_id == workspace_id,
            YjsUpdate.artifact == artifact,
            YjsUpdate.id == min_id,
        )
        .values(update_bytes=merged_update, actor_id=merged_actor_id)
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
