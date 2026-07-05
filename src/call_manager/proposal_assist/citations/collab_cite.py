# src/call_manager/proposal_assist/citations/collab_cite.py
"""Async orchestration for a direct (pc-free) human cite against the live Yjs room.
Sibling to proposals.kernel_accept.accept_via_kernel — same room-lock / threadpool /
broadcast-outside-lock / post-mutation-evict contract."""
from __future__ import annotations

import uuid

from starlette.concurrency import run_in_threadpool

from call_manager.proposal_assist.citations.human_cite import (
    _ALLOWED_STANCES,
    CiteInvalid,
    CiteRefNotFound,
    CiteStale,
)
from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.kernel import apply_citation_to_room
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.editing.save import SectionStructureError
from call_manager.proposal_assist.proposals.kernel_accept import RoomEvicted

# Pre-mutation validation errors — re-raised WITHOUT evicting the room (it is
# untouched). Mirrors _CLEAN_ACCEPT_ERRORS in kernel_accept: SectionStructureError
# (incl. its SectionMapDivergenceError subclass) can be raised by the section apply
# BEFORE the fragment mutation, so it must not needlessly evict a clean room.
_CLEAN_CITE_ERRORS = (
    CiteStale, CiteInvalid, CiteRefNotFound, CommitMutexTimeout,
    IndexError, RoomEvicted, SectionStructureError,
)


async def direct_cite_via_kernel(
    *, session_factory, workspaces_dir, workspace_id: uuid.UUID, actor_id: int,
    section_index, base_section_sha, anchor, reference_id, chunk_index,
    evidence_quote, quote_start, quote_end, stance="supports",
) -> dict:
    # Stance validation — parity with the flag-off human_cite path
    # (insert_citation_for_user raises CiteInvalid on a bad stance). compute_citation_insert
    # does NOT validate stance, so guard here before touching the room. Raised pre-op → 422.
    if stance not in _ALLOWED_STANCES:
        raise CiteInvalid(f"invalid stance {stance!r}")
    key = (workspace_id, "draft")

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(s, workspace_id=workspace_id, artifact="draft",
                                          workspaces_dir=workspaces_dir)
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> tuple[bytes | None, dict]:
                    with session_factory() as s:
                        res = apply_citation_to_room(
                            s, room_doc=room.doc, workspace_id=workspace_id,
                            section_index=section_index, base_section_sha=base_section_sha,
                            anchor=anchor, reference_id=reference_id, chunk_index=chunk_index,
                            evidence_quote=evidence_quote, quote_start=quote_start,
                            quote_end=quote_end, stance=stance, actor_id=actor_id,
                            workspaces_dir=workspaces_dir, require_anchor_in_section=True)
                        if room.evicted:
                            raise RoomEvicted(str(key))  # tombstoned mid-cite — abort before commit
                        s.commit()
                        return res.update, {"ev_id": res.ev_id, "citekey": res.citekey,
                                            "commit_sha": res.commit_sha}
                update, out = await run_in_threadpool(_body)
            if update is not None and not room.evicted:
                await room.broadcast(P.create_update_message(update))
            return out
        except _CLEAN_CITE_ERRORS:
            raise  # pre-mutation — room doc untouched, no evict
        except Exception:
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise
