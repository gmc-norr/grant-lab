# src/grant_lab/proposal_assist/citations/collab_cite.py
"""Async orchestration for a direct (pc-free) human cite against the live Yjs room.
Sibling to proposals.kernel_accept.accept_via_kernel — same room-lock / threadpool /
broadcast-outside-lock / post-mutation-evict contract."""
from __future__ import annotations

import uuid

from starlette.concurrency import run_in_threadpool

from grant_lab.proposal_assist.analysis.insert import InsertConflict
from grant_lab.proposal_assist.citations.human_cite import (
    _ALLOWED_STANCES,
    CiteInvalid,
    CiteRefNotFound,
    CiteStale,
)
from grant_lab.proposal_assist.collab import protocol as P
from grant_lab.proposal_assist.collab.hydrate import load_or_hydrate
from grant_lab.proposal_assist.collab.kernel import (
    KernelUnsafeSection,
    apply_annotation_insert_to_room,
    apply_citation_to_room,
    kernel_init_spec,
)
from grant_lab.proposal_assist.collab.room import MANAGER
from grant_lab.proposal_assist.drafting.locking import CommitMutexTimeout, WorkspaceDeleted
from grant_lab.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from grant_lab.proposal_assist.fencing import WorkspaceGenerationStale
from grant_lab.proposal_assist.ports.stamp import FenceStamp
from grant_lab.proposal_assist.proposals.kernel_accept import RoomEvicted

# Pre-mutation validation errors — re-raised WITHOUT evicting the room (it is
# untouched). Mirrors _CLEAN_ACCEPT_ERRORS in kernel_accept: SectionStructureError
# (incl. its SectionMapDivergenceError subclass) can be raised by the section apply
# BEFORE the fragment mutation, so it must not needlessly evict a clean room.
_CLEAN_CITE_ERRORS = (
    CiteStale, CiteInvalid, CiteRefNotFound, CommitMutexTimeout, WorkspaceDeleted,
    # Primitive B (endgame Task 5): raised from the kernel's
    # acquire_commit_mutex_at_generation BEFORE any room mutation — same pre-mutation,
    # non-evicting shape as WorkspaceDeleted; the app-wide handler maps it to 404.
    WorkspaceGenerationStale,
    IndexError, RoomEvicted, SectionStructureError,
    # The H5 round-trip guard refuses BEFORE the fragment mutation (review
    # 2026-10-26) — same pre-mutation, non-evicting shape.
    KernelUnsafeSection,
)

# The init-spec path raises a NARROWER set than the cite paths (no anchor/section
# work happens before its mutation), so it gets its own list rather than reusing
# _CLEAN_CITE_ERRORS — an over-broad list here would silently stop evicting a room
# that a genuine post-mutation failure had left dirty. Named (not inlined at the
# except) so the "raised before any room mutation" invariant is assertable.
_CLEAN_INIT_SPEC_ERRORS = (
    SpecLockedError, CommitMutexTimeout, WorkspaceDeleted, WorkspaceGenerationStale,
    RoomEvicted,
)


async def direct_cite_via_kernel(
    *, session_factory, workspaces_dir, stamp: FenceStamp, workspace_id: uuid.UUID,
    actor_id: int,
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
                updates = load_or_hydrate(s, stamp=stamp, workspace_id=workspace_id,
                                          artifact="draft", workspaces_dir=workspaces_dir)
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> tuple[bytes | None, dict]:
                    with session_factory() as s:
                        res = apply_citation_to_room(
                            s, stamp=stamp, room_doc=room.doc, workspace_id=workspace_id,
                            section_index=section_index, base_section_sha=base_section_sha,
                            anchor=anchor, reference_id=reference_id, chunk_index=chunk_index,
                            evidence_quote=evidence_quote, quote_start=quote_start,
                            quote_end=quote_end, stance=stance, actor_id=actor_id,
                            workspaces_dir=workspaces_dir, require_anchor_in_section=True,
                            enforce_section_sha=False)
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


async def init_spec_via_kernel(
    *, session_factory, workspaces_dir, stamp: FenceStamp, workspace_id: uuid.UUID,
    actor_id: int,
) -> tuple[str, str]:
    """'Start spec from skeleton' against the live spec room. Same
    operate/lock/threadpool/broadcast-outside-lock/evict contract as
    direct_cite_via_kernel. Returns (head_sha, room_markdown).

    CAREFUL: kernel_init_spec mutates room.doc BEFORE its checkpoint; a failure
    AFTER the mutation must evict (dirty room) — that is exactly what the bare
    `except Exception` below does. _CLEAN_INIT_SPEC_ERRORS are raised BEFORE the
    mutation so they are clean (no evict). Do not reorder kernel_init_spec so that
    stops being true.
    """
    key = (workspace_id, "spec")

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(
                    s, stamp=stamp, workspace_id=workspace_id, artifact="spec",
                    workspaces_dir=workspaces_dir,
                )
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> tuple[bytes | None, str, str]:
                    with session_factory() as s:
                        head, md, update = kernel_init_spec(
                            s, stamp=stamp, room_doc=room.doc, workspace_id=workspace_id,
                            workspaces_dir=workspaces_dir, actor_id=actor_id,
                        )
                        if room.evicted:
                            raise RoomEvicted(str(key))
                        s.commit()
                        return update, head, md
                update, head, md = await run_in_threadpool(_body)
            if update is not None and not room.evicted:
                await room.broadcast(P.create_update_message(update))
            return head, md
        except _CLEAN_INIT_SPEC_ERRORS:
            raise  # pre/clean — room doc untouched or tombstoned; no evict
        except Exception:
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise


async def insert_annotation_via_kernel(
    *, session_factory, workspaces_dir, stamp: FenceStamp, workspace_id: uuid.UUID,
    actor_id: int, annotation_id: int,
) -> dict:
    """Claims-tab annotation insert against the live draft room. Same
    operate/lock/threadpool/broadcast-outside-lock/evict contract as
    direct_cite_via_kernel — delegates the mutex/derive/compute/apply/
    checkpoint pipeline to apply_annotation_insert_to_room (the bespoke,
    occurrence-aware core; see that function's docstring for why it does NOT
    reuse apply_citation_to_room's compute).

    Returns {"ev_id", "citekey", "commit_sha"} on a genuine insert, or
    {"idempotent": True} when THIS annotation was already inserted (or a
    claim-context repair hit resolved it with no new room mutation).
    """
    key = (workspace_id, "draft")

    async def _load() -> list[bytes]:
        def _read():
            with session_factory() as s:
                updates = load_or_hydrate(s, stamp=stamp, workspace_id=workspace_id,
                                          artifact="draft", workspaces_dir=workspaces_dir)
                s.commit()
                return updates
        return await run_in_threadpool(_read)

    async with MANAGER.operate(key, load=_load) as room:
        try:
            async with room.lock:
                def _body() -> tuple[bytes | None, dict]:
                    with session_factory() as s:
                        res = apply_annotation_insert_to_room(
                            s, stamp=stamp, room_doc=room.doc, workspace_id=workspace_id,
                            workspaces_dir=workspaces_dir, annotation_id=annotation_id,
                            actor_id=actor_id,
                        )
                        if room.evicted:
                            # tombstoned mid-insert — abort before commit
                            raise RoomEvicted(str(key))
                        s.commit()
                        if res.idempotent:
                            return None, {"idempotent": True}
                        return res.update, {
                            "ev_id": res.ev_id, "citekey": res.citekey,
                            "commit_sha": res.commit_sha,
                        }
                update, out = await run_in_threadpool(_body)
            if update is not None and not room.evicted:
                await room.broadcast(P.create_update_message(update))
            return out
        except (*_CLEAN_CITE_ERRORS, InsertConflict):
            raise  # pre-mutation / tombstoned — room doc untouched, no evict
        except Exception:
            await MANAGER.evict(key)  # room.doc may be ahead of the rolled-back log
            raise
