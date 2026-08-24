# src/call_manager/proposal_assist/collab/kernel.py
"""Room-aware write kernel (Slice 2b-1): accept a prose proposal against the
LIVE Yjs room, not git. Validates staleness against the live section SHA,
applies the accepted edit as an authenticated incremental Yjs update, stages it,
and checkpoints — all on the caller's single transaction (the caller commits)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pycrdt import Doc
from sqlalchemy.orm import Session

from call_manager.db.models import (
    ProposedChange,
    Reference,
    User,
    UserClaimAnnotation,
    Workspace,
    WorkspaceEvent,
)
from call_manager.proposal_assist.analysis.insert import (
    TURN_IN_FLIGHT_MESSAGE,
    InsertConflict,
    _turn_in_flight,
    compute_annotation_insert,
    derive_annotation_cite,
)
from call_manager.proposal_assist.artifacts import SPEC_SKELETON
from call_manager.proposal_assist.citations.core import (
    _write_citekey_cache,
    compute_citation_insert,
)
from call_manager.proposal_assist.citations.human_cite import (
    CiteInvalid,
    CiteRefNotFound,
    CiteStale,
)
from call_manager.proposal_assist.citations.ledger import dump_ledger, load_ledger
from call_manager.proposal_assist.citations.markers import anchor_occurrences
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.canonical import read_head_markdown
from call_manager.proposal_assist.collab.checkpoint import (
    _head_or_empty,
    checkpoint_artifact,
    serialize_doc_to_markdown,
)
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import (
    apply_pm_json_to_fragment,
    apply_pm_section_to_fragment,
    ydoc_to_pm_json,
)
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.editing.sectionizer import is_h2_heading_line, split_sections
from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.ports.stamp import FenceStamp
from call_manager.proposal_assist.proposals.accepting import _load_scoped
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME

logger = logging.getLogger(__name__)


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


@dataclass
class AnnotationRoomResult:
    update: bytes | None   # None on idempotent hit
    ev_id: str | None
    citekey: str | None
    commit_sha: str | None
    idempotent: bool


def _live_sections(room_doc: Doc):
    return split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))


def apply_citation_to_room(
    session,
    *,
    stamp: FenceStamp,
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
    enforce_section_sha=True,
) -> CitationApplyResult:
    """Apply a citation marker to the LIVE room + co-commit the ledger. Shared by
    kernel_accept_citation (pc path) and direct_cite_via_kernel (direct path).
    Staleness is vs the LIVE section SHA (never git). Raises CiteStale/CiteInvalid/
    CiteRefNotFound; the caller decides the HTTP/pc mapping. Caller owns the txn."""
    sections = _live_sections(room_doc)
    if not 0 <= section_index < len(sections):
        raise IndexError(f"section_index {section_index} out of range")
    live = sections[section_index]
    if enforce_section_sha and live.sha != base_section_sha:
        raise CiteStale("this section changed — reopen and re-cite")
    if require_anchor_in_section and not anchor_occurrences(live.text, anchor):
        # Direct-user protection (human_cite parity): the anchor must occur in the
        # section the user selected BEFORE the whole-doc insert_marker — else a
        # whole-doc-unique anchor living in ANOTHER section would be silently
        # mis-cited. NOTE: anchor_occurrences returns list[int]; `not []` is the
        # empty-check (`== 0` would compare a list to an int and never fire).
        raise CiteStale("this section changed — reopen and re-cite")

    acquire_commit_mutex_at_generation(
        session, stamp.workspace_id, generation=stamp.generation
    )
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
    update = apply_pm_section_to_fragment(
        room_doc, section_index=section_index, blocks=section_blocks
    )
    store.append_update(session, workspace_id=workspace_id, artifact="draft",
                        actor_id=actor_id, update=update, commit=False)
    cp = checkpoint_artifact(
        session, stamp=stamp, workspace_id=workspace_id, artifact="draft",
        workspaces_dir=workspaces_dir,
        extra_files={CITATIONS_FILENAME: dump_ledger(result["new_ledger"])})
    _write_citekey_cache(session, result["ref"], result["citekey"], owns_txn=False)
    return CitationApplyResult(
        update=update, ev_id=result["ev_id"], citekey=result["citekey"],
        commit_sha=cp.commit_sha, committed=cp.committed, idempotent=False)


def kernel_accept_prose(
    session: Session,
    *,
    stamp: FenceStamp,
    room_doc: Doc,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver: User,
    workspaces_dir,
) -> KernelAcceptResult:
    # Mutex FIRST — above _load_scoped, above the staleness branch. Three reasons:
    #   (1) the staleness branch below resolves the ProposedChange to "stale" and
    #       flushes, and accept_via_kernel COMMITS — so a workspace deleted
    #       mid-turn would get a durable row mutation if we only guarded the later
    #       write path. Primitive B raises WorkspaceDeleted / WorkspaceGenerationStale
    #       (at the caller's stamped generation) before any of it.
    #   (2) it pins ONE lock order everywhere: advisory mutex -> ProposedChange row
    #       lock (_load_scoped takes FOR UPDATE), never the reverse.
    #   (3) it still precedes the spec_locked_at read below, which is what the
    #       original placement was for (Codex H3): post_lock_spec (workspaces.py:487)
    #       sets that lock UNDER this same mutex, so holding it here freezes the lock
    #       state for our whole transaction.
    # checkpoint_artifact re-acquires the same xact lock later — Postgres advisory
    # xact locks are re-entrant within a transaction, so that is an instant no-op
    # (no deadlock, no second timeout) with one redundant fresh liveness read.
    # Raised BEFORE any mutation, so it needs no room eviction (CommitMutexTimeout,
    # WorkspaceDeleted and WorkspaceGenerationStale are all in _CLEAN_ACCEPT_ERRORS).
    # The generation comes from the accept route's access check (the threaded stamp),
    # not a read taken here at the write.
    acquire_commit_mutex_at_generation(
        session, stamp.workspace_id, generation=stamp.generation
    )

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

    # (The commit mutex is already held — taken at the top of this function.)

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
        session, stamp=stamp, workspace_id=workspace_id, artifact=artifact,
        workspaces_dir=workspaces_dir,
    )
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = result.commit_sha
    session.flush()
    return KernelAcceptResult(pc=pc, update=update, committed=result.committed, evict=True)


def kernel_init_spec(
    session: Session,
    *,
    stamp: FenceStamp,
    room_doc: Doc,
    workspace_id: uuid.UUID,
    workspaces_dir,
    actor_id: int,
) -> tuple[str, str, bytes | None]:
    """Seed SPEC_SKELETON into an empty live spec room (3b-5d.D4 §3.3).

    Returns (head_sha, room_markdown, broadcast_update). broadcast_update is
    None on the idempotent no-op path (room already has content). Raises
    SpecLockedError under the mutex (mirrors post_init_spec's git path) and
    CommitMutexTimeout. Caller commits.

    Deliberate provenance deviation (spec §3.3): the commit provenance here
    comes from checkpoint_artifact (ArtifactVersion message "collab checkpoint:
    spec", WorkspaceEvent kind="collab_checkpoint") rather than the git path's
    "user spec init" / kind="user_edit" rows — do NOT duplicate provenance rows.
    """
    acquire_commit_mutex_at_generation(
        session, stamp.workspace_id, generation=stamp.generation
    )
    ws = session.get(Workspace, workspace_id)
    session.refresh(ws, ["spec_locked_at"])
    if ws.spec_locked_at is not None:
        raise SpecLockedError(str(workspace_id))

    live_md = serialize_doc_to_markdown(room_doc)
    if live_md.strip() != "":
        # Idempotent no-op: the room already has content. Head = current git
        # HEAD (the response's git anchor, same as the git path's no-op branch).
        head = _head_or_empty(workspace_repo_dir(workspaces_dir, workspace_id))
        return head, live_md, None

    update = apply_pm_json_to_fragment(room_doc, markdown_to_pm(SPEC_SKELETON))
    store.append_update(
        session, workspace_id=workspace_id, artifact="spec",
        actor_id=actor_id, update=update, commit=False,
    )
    cp = checkpoint_artifact(
        session, stamp=stamp, workspace_id=workspace_id, artifact="spec",
        workspaces_dir=workspaces_dir,
    )
    return cp.commit_sha, serialize_doc_to_markdown(room_doc), update


def kernel_accept_citation(
    session: Session,
    *,
    stamp: FenceStamp,
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
    # Mutex FIRST (see kernel_accept_prose): _stale() below flushes a RESOLVED
    # ProposedChange, and it fires on CiteStale/CiteInvalid/CiteRefNotFound —
    # which apply_citation_to_room raises ABOVE its own acquire. Guarding only
    # that inner acquire would leave this path writing to a deleted workspace.
    # Also pins the lock order: advisory mutex -> ProposedChange row lock. The
    # generation comes from the accept route's access check (the threaded stamp);
    # the inner apply_citation_to_room re-acquires with the SAME stamp, a re-entrant
    # no-op on the mutex.
    acquire_commit_mutex_at_generation(
        session, stamp.workspace_id, generation=stamp.generation
    )

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
            session, stamp=stamp, room_doc=room_doc, workspace_id=workspace_id,
            section_index=pc.section_index, base_section_sha=pc.base_section_sha,
            anchor=p["anchor"], reference_id=p["reference_id"], chunk_index=p["chunk_index"],
            evidence_quote=p["evidence_quote"], quote_start=None, quote_end=None,
            stance=p.get("stance", "supports"), confidence=p.get("confidence"),
            actor_id=approver.id, workspaces_dir=workspaces_dir,
            require_anchor_in_section=False)  # pc was section-validated at propose time
    except (CiteStale, CiteInvalid, CiteRefNotFound):
        return _stale()  # git-first parity: every verify failure is terminal-stale
    if res.idempotent:
        pc.status = "accepted"
        pc.resolved_by = approver.id
        pc.resolved_at = datetime.now(UTC)
        pc.resulting_commit_sha = res.commit_sha
        session.flush()
        return KernelAcceptResult(pc=pc, update=None, committed=False, evict=False)
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = res.commit_sha
    session.flush()
    return KernelAcceptResult(pc=pc, update=res.update, committed=res.committed, evict=True)


def apply_annotation_insert_to_room(
    session: Session,
    *,
    stamp: FenceStamp,
    room_doc: Doc,
    workspace_id: uuid.UUID,
    workspaces_dir,
    annotation_id: int,
    actor_id: int,
) -> AnnotationRoomResult:
    """Claims-tab insert against the live room (3b-5d.D4 §3.3). Sequence:
    load ws+annotation -> turn-lock guard -> derive vs the ROOM-serialized doc
    -> compute_annotation_insert (occurrence-aware; claim-ctx repair) ->
    [repair: return idempotent, NO room mutation] -> apply new_doc to the
    fragment -> append_update(commit=False) -> checkpoint(extra_files=ledger)
    -> user_citation_insert WorkspaceEvent + annotation resolved fields +
    citekey cache. Caller commits. Raises InsertConflict/CommitMutexTimeout
    exactly like the git path.

    Bespoke core (NOT apply_citation_to_room's compute_citation_insert): this
    insert is occurrence-aware — the same (reference_id, chunk_index,
    evidence_quote) already live at ANOTHER sentence must NOT satisfy THIS
    annotation. compute_citation_insert's generic _find_idempotent_row scan
    would falsely short-circuit that case, so derive_annotation_cite /
    compute_annotation_insert (analysis/insert.py) are reused instead — this
    function mirrors ONLY apply_citation_to_room's room MECHANICS (mutex,
    ledger read, fragment apply, append_update, checkpoint, citekey cache).
    """
    ws = session.get(Workspace, workspace_id)
    ann = session.get(UserClaimAnnotation, annotation_id)
    if ann is None or ann.workspace_id != workspace_id:
        raise InsertConflict("annotation is no longer actionable")

    acquire_commit_mutex_at_generation(
        session, stamp.workspace_id, generation=stamp.generation
    )

    # Parity with insert.py step 1: re-read status inside the mutex. A
    # concurrent insert that committed while we were blocked would have
    # flipped ann.status to "inserted" — expire so the next attribute access
    # re-SELECTs under READ COMMITTED.
    session.expire(ann)

    if _turn_in_flight(session, workspace_id):
        raise InsertConflict(TURN_IN_FLIGHT_MESSAGE)

    # Whitespace-canonicalization guard: the room's markdown is a PM round-trip
    # of whatever seeded it, which canonicalizes inter-block whitespace (e.g. a
    # single newline between a heading and its body becomes a blank line —
    # ProseMirror's block model has no representation for "how many newlines
    # separated two blocks"). An annotation is analyzed against RAW git text
    # (the claim pipeline never consults collab state), so its section_sha can
    # never equal a room-derived sha even when nothing has actually changed.
    # It is only SAFE to derive against raw git HEAD instead of the room's text
    # while the room has had NO real (authored) update yet: collab severance
    # guarantees no other writer can move git HEAD while this artifact is
    # collab-enabled, so an untouched room (hydration seed only, actor_id IS
    # NULL) is PROVABLY just a re-serialization of the CURRENT git HEAD — using
    # HEAD's raw text for section/anchor matching is exactly as safe as the git
    # path's own matching, and the result is still applied into the LIVE
    # fragment below. Once a real edit has landed, this falls back to the
    # room's own text — a stale pre-flip annotation legitimately becomes
    # unmatched until reanalysis, the same degradation the Claims-tab read
    # model (annotations.py get_annotations) also accepts. "Untouched" is the
    # SAME predicate the read model gates on — store.has_authored_updates —
    # so the two paths can never disagree about which text a section_sha was
    # hashed against.
    live_md = serialize_doc_to_markdown(room_doc)
    doc_for_derive = live_md
    repo = workspace_repo_dir(workspaces_dir, workspace_id)
    untouched = not store.has_authored_updates(
        session, workspace_id=workspace_id, artifact="draft"
    )
    if untouched:
        head_text = read_head_markdown(repo, "draft")
        if head_text is not None:
            doc_for_derive = head_text

    derived = derive_annotation_cite(session, workspace=ws, annotation=ann, doc=doc_for_derive)
    if derived is None:
        return AnnotationRoomResult(
            update=None, ev_id=None, citekey=None, commit_sha=None, idempotent=True
        )

    citations_path = repo / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        logger.error("corrupt citations.json in workspace %s: %s", workspace_id, exc)
        raise InsertConflict(f"citations.json is invalid: {exc}") from exc

    outcome = compute_annotation_insert(
        session, workspace=ws, annotation=ann, user_id=actor_id,
        doc=doc_for_derive, ledger=ledger, derived=derived,
    )
    if outcome.repair is not None:
        # Claim-context repair hit: compute_annotation_insert already flipped
        # ann.status/resolved_* — nothing new was written, no room mutation.
        rid, ck = outcome.repair
        return AnnotationRoomResult(
            update=None, ev_id=rid, citekey=ck, commit_sha=None, idempotent=True
        )

    # Section-scoped fragment replace (mirrors apply_citation_to_room, NOT a
    # whole-doc replace): the target section is the ONLY one derive_annotation_cite
    # touched, so this preserves every other section's Yjs identity + any
    # concurrent edit exactly like the citation-insert kernel path does.
    new_section_md = split_sections(outcome.new_doc)[derived.section_index].text
    section_blocks = markdown_to_pm(new_section_md).get("content") or []
    update = apply_pm_section_to_fragment(
        room_doc, section_index=derived.section_index, blocks=section_blocks
    )
    store.append_update(
        session, workspace_id=workspace_id, artifact="draft",
        actor_id=actor_id, update=update, commit=False,
    )
    cp = checkpoint_artifact(
        session, stamp=stamp, workspace_id=workspace_id, artifact="draft",
        workspaces_dir=workspaces_dir,
        extra_files={CITATIONS_FILENAME: dump_ledger(outcome.new_ledger)},
        # Suppress the checkpoint's own collab_checkpoint WorkspaceEvent (final-
        # review Minor 5): this op emits its OWN user_citation_insert event just
        # below, so recording both would render two feed rows ("saved a
        # version" + "added a citation") for one click. The ArtifactVersion
        # still lands (record_version stays True) — only the redundant feed
        # row is dropped.
        record_event=False,
    )

    # Provenance parity with insert.py step 10 (deliberately NO ArtifactVersion
    # here — checkpoint_artifact already recorded one for this commit, same
    # deviation as kernel_init_spec).
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="user_citation_insert",
            actor_id=actor_id,
            payload={"annotation_id": ann.id, "ev_id": outcome.ev_id, "citekey": outcome.citekey},
        )
    )
    ann.status = "inserted"
    ann.resolved_by_user_id = actor_id
    ann.resolved_at = datetime.now(UTC)

    ref = session.get(Reference, derived.reference_id)
    _write_citekey_cache(session, ref, outcome.citekey, owns_txn=False)

    return AnnotationRoomResult(
        update=update, ev_id=outcome.ev_id, citekey=outcome.citekey,
        commit_sha=cp.commit_sha, idempotent=False,
    )
