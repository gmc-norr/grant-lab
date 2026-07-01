# src/call_manager/proposal_assist/proposals/kernel_accept.py
"""Async orchestration for a room-aware proposal accept (Slice 2b-1)."""

from __future__ import annotations

import uuid

from starlette.concurrency import run_in_threadpool

from call_manager.db.models import ProposedChange, User
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.kernel import (
    CitationKernelDeferred,
    KernelAcceptResult,
    kernel_accept_prose,
)
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending

# These are all raised BEFORE any fragment mutation (validation guards, plus the
# commit-mutex acquire that precedes the spec-lock guard — Codex H3), so the room
# doc is untouched and there is nothing to evict. Every OTHER exception may have
# left room.doc mutated while the txn rolled back → evict to rebuild from the log.
_CLEAN_ACCEPT_ERRORS = (
    SpecLockedError,
    SectionStructureError,
    CitationKernelDeferred,
    ProposedChangeNotPending,
    CommitMutexTimeout,
    IndexError,
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
                        res = kernel_accept_prose(
                            s, room_doc=room.doc, proposed_change_id=pc_id,
                            workspace_id=workspace_id, approver=approver,
                            workspaces_dir=workspaces_dir,
                        )
                        # Commit the ONE transaction, then detach the pc for return.
                        s.commit()
                        s.refresh(res.pc)
                        s.expunge(res.pc)
                        return res

                result = await run_in_threadpool(_body)
                if result.update is not None:
                    await room.broadcast(P.create_update_message(result.update))
            return result.pc
        except _CLEAN_ACCEPT_ERRORS:
            raise  # validation error before any mutation — room doc untouched
        except Exception:
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise
