# src/call_manager/proposal_assist/collab/kernel.py
"""Room-aware write kernel (Slice 2b-1): accept a prose proposal against the
LIVE Yjs room, not git. Validates staleness against the live section SHA,
applies the accepted edit as an authenticated incremental Yjs update, stages it,
and checkpoints — all on the caller's single transaction (the caller commits)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pycrdt import Doc
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange, User, Workspace
from call_manager.proposal_assist.citations.ledger import dump_ledger, load_ledger
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.checkpoint import _head_or_empty, checkpoint_artifact
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_json_to_fragment,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.editing.sectionizer import is_h2_heading_line, split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.proposals.accepting import _load_scoped
from call_manager.proposal_assist.tools.citations import (
    CITATIONS_FILENAME,
    _write_citekey_cache,
    compute_citation_insert,
)


@dataclass
class KernelAcceptResult:
    pc: ProposedChange
    update: bytes | None
    committed: bool
    evict: bool


def _live_sections(room_doc: Doc):
    return split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))


def kernel_accept_prose(
    session: Session,
    *,
    room_doc: Doc,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver: User,
    workspaces_dir,
) -> KernelAcceptResult:
    pc = _load_scoped(session, proposed_change_id, workspace_id)  # FOR UPDATE
    if pc.status != "pending":
        from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending

        raise ProposedChangeNotPending(pc.status)

    artifact = pc.target_artifact
    sections = _live_sections(room_doc)
    if not 0 <= pc.section_index < len(sections):
        raise IndexError(f"section_index {pc.section_index} out of range")
    live = sections[pc.section_index]

    # THE regression guard: staleness vs the LIVE section SHA (never git HEAD).
    if live.sha != pc.base_section_sha:
        pc.status = "stale"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Acquire the commit mutex NOW — before reading spec_locked_at (Codex H3).
    # post_lock_spec (workspaces.py:487) sets the lock UNDER this same mutex; checking
    # spec_locked_at before taking the mutex would let a concurrent lock commit between
    # our guard and our checkpoint, accepting into a spec that just got locked. Taking
    # it here freezes the lock state for the rest of our transaction. checkpoint_artifact
    # re-acquires the same xact lock later — Postgres advisory xact locks are re-entrant
    # within a transaction, so that is an instant no-op (no deadlock, no second timeout).
    # Raised BEFORE any doc mutation, so a CommitMutexTimeout needs no room eviction
    # (it is in _CLEAN_ACCEPT_ERRORS).
    acquire_commit_mutex(session, workspace_id)

    # Spec-lock guard (the lock OPERATION is 2b-2; here we only honor an existing lock).
    if artifact == "spec":
        ws = session.get(Workspace, workspace_id)
        session.refresh(ws, ["spec_locked_at"])
        if ws.spec_locked_at is not None:
            raise SpecLockedError(str(workspace_id))

    new_text = pc.payload["new_text"]

    # Structure guard (mirror save.py): non-preamble edits start with an H2 heading;
    # the preamble (index 0) must not.
    first_line = new_text.split("\n", 1)[0] if new_text else ""
    if pc.section_index > 0:
        if not is_h2_heading_line(first_line):
            raise SectionStructureError(
                f"section {pc.section_index} edit must start with an H2 heading (## …)"
            )
    elif new_text and is_h2_heading_line(first_line):
        raise SectionStructureError("preamble (section 0) edit must not start with an H2 heading")

    # Newline glue for a non-final section (mirror save.py).
    if pc.section_index < len(sections) - 1 and not new_text.endswith("\n"):
        new_text += "\n"

    # No-op: proposing exactly the live text — accept at the current checkpoint,
    # no new commit (mirrors save_section's changed=False short-circuit).
    if new_text == live.text:
        repo = workspace_repo_dir(workspaces_dir, workspace_id)
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = _head_or_empty(repo)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Apply: reassemble the whole doc with the section replaced, mutate the live
    # room fragment (capturing the incremental update), stage it, checkpoint.
    new_doc_md = "".join(new_text if s.index == pc.section_index else s.text for s in sections)
    update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(new_doc_md))
    # From here on room_doc is mutated: any failure must evict the room.
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact,
        actor_id=approver.id, update=update, commit=False,
    )
    result = checkpoint_artifact(
        session, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
    )
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = result.commit_sha
    session.flush()
    return KernelAcceptResult(pc=pc, update=update, committed=result.committed, evict=True)


def kernel_accept_citation(
    session: Session,
    *,
    room_doc: Doc,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver: User,
    workspaces_dir,
) -> KernelAcceptResult:
    """Accept a citation_insert against the LIVE Yjs room and co-commit the ledger.

    Sibling to ``kernel_accept_prose``. Applies the marker-bearing whole-doc
    markdown to the live room fragment as an authenticated incremental Yjs update,
    then checkpoints proposal.md + citations.json in ONE ``write_and_commit_files``
    (via ``extra_files``) so a git failure rolls back BOTH the room marker and the
    ledger row. Staleness is checked against the LIVE section SHA (never git). The
    caller owns the transaction (the route's ``_body`` commits once); this function
    only ``flush()``es. All verify failures map to terminal stale (git-first parity).
    """
    pc = _load_scoped(session, proposed_change_id, workspace_id)  # FOR UPDATE
    if pc.status != "pending":
        from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending

        raise ProposedChangeNotPending(pc.status)

    artifact = pc.target_artifact
    sections = _live_sections(room_doc)
    if not 0 <= pc.section_index < len(sections):
        raise IndexError(f"section_index {pc.section_index} out of range")
    live = sections[pc.section_index]

    def _stale() -> KernelAcceptResult:
        pc.status = "stale"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Staleness vs the LIVE section SHA (never git) — identical to prose.
    if live.sha != pc.base_section_sha:
        return _stale()

    # Freeze the writer BEFORE minting/reading the ledger (Codex H3). Re-entered by
    # checkpoint_artifact as an in-txn no-op (Postgres advisory xact locks are
    # re-entrant within a transaction). Raised BEFORE any doc mutation.
    acquire_commit_mutex(session, workspace_id)

    # The ledger is disk-canonical (NOT in the room); read it under the mutex via
    # the canonical loader (load_ledger(None) -> empty_ledger()).
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    _cpath = repo / CITATIONS_FILENAME
    try:
        ledger = load_ledger(_cpath.read_text() if _cpath.exists() else None)
    except ValueError:
        # Corrupt citations.json. The git-first path (_read_proposal_and_ledger,
        # citations.py) turns this into {"error": ...} which maps to terminal stale
        # — mirror that here (parity) rather than letting the ValueError escape to
        # the generic post-mutation evict arm (room eviction + 500). Codex Medium.
        return _stale()

    room_md = pm_to_markdown(ydoc_to_pm_json(room_doc))  # whole-doc, room-canonical
    p = pc.payload
    result = compute_citation_insert(
        session,
        workspace_id=workspace_id,
        proposal_text=room_md,
        ledger=ledger,
        ref_uuid=uuid.UUID(str(p["reference_id"])),
        anchor=p["anchor"],
        chunk_index=p["chunk_index"],
        evidence_quote=p["evidence_quote"],
        # Match the git-first accept EXACTLY (accepting.py: `payload.get("stance",
        # "supports")`) — a key-absent default, NOT `or`. A present-but-None stance
        # (the propose-time validator permits None) must map identically on both
        # paths, else flag-on and flag-off diverge (parity is the point of 2b-2).
        stance=p.get("stance", "supports"),
        confidence=p.get("confidence"),
    )
    if "error" in result:
        # All verify failures are terminal-stale (git-first parity).
        return _stale()
    if result.get("idempotent"):
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = _head_or_empty(repo)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    # Apply the marker-bearing whole-doc markdown to the live room fragment,
    # capturing the incremental update, stage it, and checkpoint proposal.md +
    # citations.json in ONE commit (extra_files).
    update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(result["new_proposal_text"]))
    # From here on room_doc is mutated: any failure must evict the room.
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact,
        actor_id=approver.id, update=update, commit=False,
    )
    cp = checkpoint_artifact(
        session, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir,
        extra_files={CITATIONS_FILENAME: dump_ledger(result["new_ledger"])},
    )
    # Post-commit citekey cache (Codex High) — owns_txn=False: begin_nested()+flush
    # so the FOR UPDATE pc-row lock + advisory mutex stay held; the route's single
    # session.commit persists the checkpoint + status flip + this cache write in ONE
    # transaction. Same helper + semantics as the git-first accept-replay.
    _write_citekey_cache(session, result["ref"], result["citekey"], owns_txn=False)
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = cp.commit_sha
    session.flush()
    return KernelAcceptResult(pc=pc, update=update, committed=cp.committed, evict=True)
