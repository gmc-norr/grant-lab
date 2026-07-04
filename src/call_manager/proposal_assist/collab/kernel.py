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
    apply_pm_section_to_fragment,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.editing.sectionizer import is_h2_heading_line, split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.citations.human_cite import CiteInvalid, CiteRefNotFound, CiteStale
from call_manager.proposal_assist.citations.markers import anchor_occurrences
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


@dataclass
class CitationApplyResult:
    update: bytes | None   # None on idempotent hit
    ev_id: str
    citekey: str | None
    commit_sha: str
    committed: bool
    idempotent: bool


def _live_sections(room_doc: Doc):
    return split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))


def apply_citation_to_room(
    session,
    *,
    room_doc,
    workspace_id,
    section_index,
    base_section_sha,
    anchor,
    reference_id,
    chunk_index,
    evidence_quote,
    quote_start=None,
    quote_end=None,
    stance="supports",
    confidence=None,
    actor_id,
    workspaces_dir,
    require_anchor_in_section=False,
) -> CitationApplyResult:
    """Apply a citation marker to the LIVE room + co-commit the ledger. Shared by
    kernel_accept_citation (pc path) and direct_cite_via_kernel (direct path).
    Staleness is vs the LIVE section SHA (never git). Raises CiteStale/CiteInvalid/
    CiteRefNotFound; the caller decides the HTTP/pc mapping. Caller owns the txn."""
    sections = _live_sections(room_doc)
    if not 0 <= section_index < len(sections):
        raise IndexError(f"section_index {section_index} out of range")
    live = sections[section_index]
    if live.sha != base_section_sha:
        raise CiteStale("this section changed — reopen and re-cite")
    if require_anchor_in_section and not anchor_occurrences(live.text, anchor):
        # Direct-user protection (human_cite parity): the anchor must occur in the
        # section the user selected BEFORE the whole-doc insert_marker — else a
        # whole-doc-unique anchor living in ANOTHER section would be silently
        # mis-cited. NOTE: anchor_occurrences returns list[int]; `not []` is the
        # empty-check (`== 0` would compare a list to an int and never fire).
        raise CiteStale("this section changed — reopen and re-cite")

    acquire_commit_mutex(session, workspace_id)
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    _cpath = repo / CITATIONS_FILENAME
    try:
        ledger = load_ledger(_cpath.read_text() if _cpath.exists() else None)
    except ValueError as exc:
        raise CiteInvalid("citations.json is invalid") from exc

    room_md = pm_to_markdown(ydoc_to_pm_json(room_doc))
    result = compute_citation_insert(
        session, workspace_id=workspace_id, proposal_text=room_md, ledger=ledger,
        ref_uuid=uuid.UUID(str(reference_id)), anchor=anchor, chunk_index=chunk_index,
        evidence_quote=evidence_quote, stance=stance, confidence=confidence,
        quote_start=quote_start, quote_end=quote_end)
    if "error" in result:
        code = result.get("error_code")
        msg = result["error"]
        # Map by TYPED CODE (not message substrings) to mirror human_cite's HTTP
        # semantics: ref→404, anchor-moved→409, everything else (chunk/quote/
        # ambiguous-anchor)→422. The PC path (kernel_accept_citation) still catches
        # all three and collapses them to _stale(), so its parity is unchanged.
        if code == "ref_not_found":
            raise CiteRefNotFound(msg)   # 404
        if code == "anchor_not_found":
            raise CiteStale(msg)         # 409 — section moved / anchor gone
        raise CiteInvalid(msg)           # 422 — chunk / quote / anchor_ambiguous
    if result.get("idempotent"):
        return CitationApplyResult(
            update=None, ev_id=result["ev_id"], citekey=result.get("citekey"),
            commit_sha=_head_or_empty(repo), committed=False, idempotent=True)

    new_section_md = split_sections(result["new_proposal_text"])[section_index].text
    section_blocks = markdown_to_pm(new_section_md).get("content") or []
    update = apply_pm_section_to_fragment(room_doc, section_index=section_index, blocks=section_blocks)
    store.append_update(session, workspace_id=workspace_id, artifact="draft",
                        actor_id=actor_id, update=update, commit=False)
    cp = checkpoint_artifact(
        session, workspace_id=workspace_id, artifact="draft", workspaces_dir=workspaces_dir,
        extra_files={CITATIONS_FILENAME: dump_ledger(result["new_ledger"])})
    _write_citekey_cache(session, result["ref"], result["citekey"], owns_txn=False)
    return CitationApplyResult(
        update=update, ev_id=result["ev_id"], citekey=result["citekey"],
        commit_sha=cp.commit_sha, committed=cp.committed, idempotent=False)


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

    # Apply ONLY the target section's blocks to the live room fragment (Slice 3a
    # C2): the staleness gate above proved this section unchanged since the
    # proposer read it, so a section-scoped replace clobbers nothing on the target
    # and preserves concurrent edits in every OTHER section. An empty new_text is a
    # legal preamble clear (see the structure guard above) — raw markdown_to_pm
    # raises on blank input, so map it to NO blocks.
    section_blocks = markdown_to_pm(new_text).get("content") or [] if new_text.strip() else []
    update = apply_pm_section_to_fragment(
        room_doc, section_index=pc.section_index, blocks=section_blocks
    )
    if not update:
        # No net change to the fragment (e.g. a blank edit of an already-empty
        # range). Persisting b"" would poison the durable log — pycrdt
        # apply_update(b"") raises on the next hydration. Accept as a no-op at the
        # current checkpoint (mirrors the new_text == live.text short-circuit above).
        repo = workspace_repo_dir(workspaces_dir, workspace_id)
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = _head_or_empty(repo)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)
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

    Sibling to ``kernel_accept_prose``. Delegates the mutex→ledger→compute→apply→
    checkpoint pipeline to ``apply_citation_to_room``; all verify failures map to
    terminal stale (git-first parity). Caller owns the transaction.
    """
    pc = _load_scoped(session, proposed_change_id, workspace_id)  # FOR UPDATE
    if pc.status != "pending":
        from call_manager.proposal_assist.proposals.accepting import ProposedChangeNotPending

        raise ProposedChangeNotPending(pc.status)

    def _stale() -> KernelAcceptResult:
        pc.status = "stale"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)

    p = pc.payload
    try:
        res = apply_citation_to_room(
            session, room_doc=room_doc, workspace_id=workspace_id,
            section_index=pc.section_index, base_section_sha=pc.base_section_sha,
            anchor=p["anchor"], reference_id=p["reference_id"], chunk_index=p["chunk_index"],
            evidence_quote=p["evidence_quote"], quote_start=None, quote_end=None,
            stance=p.get("stance", "supports"), confidence=p.get("confidence"),
            actor_id=approver.id, workspaces_dir=workspaces_dir,
            require_anchor_in_section=False)  # pc was section-validated at propose time
    except (CiteStale, CiteInvalid, CiteRefNotFound):
        return _stale()  # git-first parity: every verify failure is terminal-stale
    if res.idempotent:
        pc.status = "accepted"; pc.resolved_by = approver.id; pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = res.commit_sha; session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)
    pc.status = "accepted"; pc.resolved_by = approver.id; pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = res.commit_sha; session.flush()
    return KernelAcceptResult(pc=pc, update=res.update, committed=res.committed, evict=True)
