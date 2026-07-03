# src/call_manager/proposal_assist/proposals/kernel_accept.py
"""Async orchestration for a room-aware proposal accept (Slice 2b-1)."""

from __future__ import annotations

import uuid

from starlette.concurrency import run_in_threadpool

from call_manager.db.models import ProposedChange, User
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.kernel import (
    KernelAcceptResult,
    kernel_accept_citation,
    kernel_accept_prose,
)
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.proposals.accepting import (
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    UnsupportedKernelKind,
)


class RoomEvicted(Exception):
    """The room was evicted (poison recovery) while this accept was in flight.
    Pre-commit abort: the pc stays pending; a fresh connect rebuilds from the log."""


# These are all raised BEFORE any fragment mutation (validation guards, plus the
# commit-mutex acquire that precedes the spec-lock guard — Codex H3), so the room
# doc is untouched and there is nothing to evict. Every OTHER exception may have
# left room.doc mutated while the txn rolled back → evict to rebuild from the log.
# ProposedChangeNotFound is raised by _load_scoped as the very first thing it does
# (a TOCTOU between _route's s.get and the kernel's FOR UPDATE load), so the room
# doc is always untouched when it propagates.
# RoomEvicted is raised immediately before s.commit() (mutation already applied to
# room.doc but the txn is rolled back on raise): the room is detached and clients
# are already being closed, so no evict is triggered here — it IS the evict signal.
_CLEAN_ACCEPT_ERRORS = (
    SpecLockedError,
    SectionStructureError,
    UnsupportedKernelKind,
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    CommitMutexTimeout,
    IndexError,
    RoomEvicted,
)


async def accept_via_kernel(
    *, session_factory, workspaces_dir, workspace_id: uuid.UUID, pc_id: uuid.UUID,
    approver: User, artifact: str,
) -> ProposedChange:
    key = (workspace_id, artifact)

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(
                    s, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
                )
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> KernelAcceptResult:
                    with session_factory() as s:
                        pc0 = s.get(ProposedChange, pc_id)
                        kind = pc0.kind if pc0 is not None else None
                        if kind == "citation_insert":
                            res = kernel_accept_citation(
                                s, room_doc=room.doc, proposed_change_id=pc_id,
                                workspace_id=workspace_id, approver=approver,
                                workspaces_dir=workspaces_dir)
                        elif kind == "prose_edit":
                            res = kernel_accept_prose(
                                s, room_doc=room.doc, proposed_change_id=pc_id,
                                workspace_id=workspace_id, approver=approver,
                                workspaces_dir=workspaces_dir)
                        else:
                            # citation_remove / citation_update / unknown — no kernel path.
                            # Raised BEFORE any room mutation → non-evicting (Codex Low).
                            raise UnsupportedKernelKind(kind)
                        if room.evicted:
                            raise RoomEvicted(str(key))  # tombstoned mid-accept — abort, pc pending
                        # Commit the ONE transaction, then detach the pc for return.
                        # expire_on_commit=False keeps the pc's columns populated
                        # after commit and _serialize reads only columns, so no
                        # refresh is needed (mirrors the git-first path, which
                        # serializes post-commit without one).
                        s.commit()
                        s.expunge(res.pc)
                        return res

                result = await run_in_threadpool(_body)
            # Broadcast OUTSIDE room.lock (mirror Room.handle's write path,
            # room.py: "a slow client must not block other writers"). The update is
            # already committed and CRDT tolerates reordering, so releasing the lock
            # before the sequential per-client sends avoids head-of-line stalling
            # every concurrent read/write on this room behind one backpressured client.
            if result.update is not None and not room.evicted:
                await room.broadcast(P.create_update_message(result.update))
            return result.pc
        except _CLEAN_ACCEPT_ERRORS:
            raise  # validation error BEFORE any mutation — room doc untouched, no evict
        except Exception:
            # POST-mutation failure only: the fragment was already mutated but the
            # txn rolled back, so room.doc is ahead of the durable log. Evict to
            # force every client to reconnect and rebuild cleanly from the log.
            # Blast radius = all clients on this room; acceptable because (a) accept
            # failures here are rare, (b) reconnect-from-log is lossless (Slice 3a e;
            # re-hydrate-in-place is deferred to a later slice). Pre-mutation trips
            # are enumerated in _CLEAN_ACCEPT_ERRORS above and never reach here.
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise
