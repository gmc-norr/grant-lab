"""User-initiated citation insert from an annotation. Canonical path ONLY
(spec §4): evidence ledger row + [@citekey]<!-- ev:id --> marker, one atomic
commit of proposal.md + citations.json. Occurrence-aware idempotency: no-op
only when THIS annotation is already inserted (same evidence live elsewhere
does NOT satisfy it — spec rev-3 finding 4).

Caller owns the transaction: provenance rows are added but NOT committed
here, and the commit mutex is released by the caller's COMMIT/ROLLBACK
(mirrors editing/save.py). A DB failure AFTER the git commit is recovered
by reconciliation (same atomicity model as save_section).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from call_manager.db.models import (
    ArtifactVersion,
    Reference,
    ReferenceChunk,
    Workspace,
    WorkspaceEvent,
)
from call_manager.proposal_assist.citations.gitio import write_and_commit_files
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    dump_ledger,
    load_ledger,
    new_ev_id,
)
from call_manager.proposal_assist.citations.markers import AnchorError, insert_marker
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
    normalize_for_quote_match,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME, _turn_in_flight
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

# Intra-package private imports — house precedent: tools/audit.py imports
# _read_proposal_and_ledger from tools.citations the same way. These are the
# canonical agent-insert pieces; reusing them keeps ONE citekey resolution and
# ONE "Family Year" snapshot for both the agent and the user insert paths.
from call_manager.proposal_assist.tools.citations import (
    CITATIONS_FILENAME,
    _citation_snapshot,
    _resolve_citekey_for_ref,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from call_manager.db.models import UserClaimAnnotation

logger = logging.getLogger(__name__)

# Sentence terminators — same set claims.py uses, so the anchored sentence
# agrees with the pipeline's iter_proposal_sentences/claim_sentence_around.
_SENTENCE_TERMINATORS = ".!?\n"
# Anchor disambiguation: extend leftward by this many chars of section
# context, at most this many times, before giving up.
_ANCHOR_EXTEND_STEP = 40
_MAX_ANCHOR_TRIES = 4


class InsertConflict(RuntimeError):
    """The insert cannot proceed (state moved underneath the annotation).
    The message is user-facing — web routes map this to 409 detail=str(exc)."""


def insert_from_annotation(
    *,
    session: Session,
    workspace: Workspace,
    user_id: int,
    annotation: UserClaimAnnotation,
    workspaces_dir: Path | str,
) -> dict:
    """Insert the citation an ``uncited_supported`` annotation suggests.

    Returns ``{"ev_id", "citekey", "commit_sha"}`` on success, or
    ``{"idempotent": True}`` when THIS annotation was already inserted.
    Raises :class:`InsertConflict` for every expected user-facing failure,
    ``CommitMutexTimeout`` (web maps to 503) and ``GitCommitError`` (500).
    """
    # 1. Serialize against every other git mutation of this workspace.
    # CommitMutexTimeout propagates — the web layer maps it to 503.
    acquire_commit_mutex(session, workspace.id)

    # 2. Turn-lock: an agent turn in flight owns the repo.
    if _turn_in_flight(session, workspace.id):
        raise InsertConflict("assistant is working — try again shortly")

    # 3. Occurrence-aware idempotency (spec rev-3 finding 4): only THIS
    # annotation being inserted is a no-op. The same (ref, chunk, quote)
    # being live elsewhere in the document does NOT satisfy this occurrence,
    # so there is deliberately NO _find_idempotent_row-style ledger scan here.
    if annotation.status == "inserted":
        return {"idempotent": True}
    if annotation.status != "open" or annotation.kind != "uncited_supported":
        raise InsertConflict("annotation is no longer actionable")

    # 4. Reference + chunk, workspace-scoped. reference_id is SET NULL on a
    # hard delete, so None means the suggested reference is gone.
    if annotation.reference_id is None:
        raise InsertConflict("the suggested reference is no longer available")
    ref = session.get(Reference, annotation.reference_id)
    if ref is None or ref.workspace_id != workspace.id or ref.tombstoned_at is not None:
        raise InsertConflict("the suggested reference is no longer available")
    if annotation.chunk_index is None or not annotation.evidence_quote:
        raise InsertConflict("the supporting excerpt is no longer available")
    chunk = (
        session.query(ReferenceChunk)
        .filter(
            ReferenceChunk.reference_id == ref.id,
            ReferenceChunk.chunk_index == annotation.chunk_index,
            ReferenceChunk.workspace_id == workspace.id,
        )
        .one_or_none()
    )
    if chunk is None:
        raise InsertConflict("the supporting excerpt is no longer available")
    # Quote-verify against the live chunk text BEFORE any document work — the
    # pipeline verified it at analysis time, but a re-chunk can invalidate it.
    try:
        q_start, q_end = find_quote_offsets(chunk.text, annotation.evidence_quote)
    except (QuoteNotFound, QuoteAmbiguous) as exc:
        raise InsertConflict("the supporting excerpt is no longer available") from exc

    # 5. Live document — the annotation addresses a section SNAPSHOT
    # (section_sha) plus a duplicate-section ordinal (section_occurrence).
    # If that exact content no longer exists at HEAD the finding is stale.
    repo = workspace_repo_dir(workspaces_dir, workspace.id)
    proposal_path = repo / PROPOSAL_FILENAME
    if not proposal_path.exists():
        raise InsertConflict("the text changed — re-analysis is pending")
    doc = proposal_path.read_text()
    sections = split_sections(doc)
    matches = [s for s in sections if s.sha == annotation.section_sha]
    if annotation.section_occurrence >= len(matches):
        raise InsertConflict("the text changed — re-analysis is pending")
    target = matches[annotation.section_occurrence]

    # 6. Build the anchor: the raw sentence slice from the annotation's
    # char_offset to (and including) the first terminator. If the slice is
    # not unique in the document, extend leftward with section context; a
    # byte-identical duplicate section can never disambiguate this way (the
    # extension never leaves target.text), so it always fails cleanly.
    start = annotation.char_offset
    if not 0 <= start < len(target.text):
        raise InsertConflict("the text changed — re-analysis is pending")
    end = start
    while end < len(target.text) and target.text[end] not in _SENTENCE_TERMINATORS:
        end += 1
    if end < len(target.text):
        end += 1  # include the terminator so the marker trails the sentence
    anchor = target.text[start:end].rstrip("\n")
    ext = start
    tries = 0
    while doc.count(anchor) != 1 and tries < _MAX_ANCHOR_TRIES:
        ext = max(0, ext - _ANCHOR_EXTEND_STEP)
        anchor = target.text[ext:end].rstrip("\n")
        tries += 1
    if doc.count(anchor) != 1:
        raise InsertConflict("repeated text — cite it via chat or edit the sentence to be unique")

    # 7. Ledger + citekey (same resolution as the agent path) + marker.
    citations_path = repo / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        raise InsertConflict(f"citations.json is invalid: {exc}") from exc
    ev_id = new_ev_id()
    citekey = _resolve_citekey_for_ref(session, ledger=ledger, ref=ref, workspace_id=workspace.id)
    try:
        new_doc = insert_marker(doc, anchor, citekey, ev_id)
    except AnchorError as exc:
        raise InsertConflict(str(exc)) from exc

    # 8. Evidence row via the single authoritative constructor — identical
    # schema shape to the agent insert (_insert_citation_locked step 12).
    now_iso = datetime.now(UTC).isoformat()
    claim_text = annotation.claim_text
    row = build_evidence_row(
        ev_id=ev_id,
        citekey=citekey,
        reference_id=str(ref.id),
        chunk_index=annotation.chunk_index,
        section_path=chunk.section_path,
        source_content_hash=ref.original_sha256,
        chunk_content_hash=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
        evidence_quote=annotation.evidence_quote,
        evidence_quote_offsets=[q_start, q_end],
        stance="supports",
        confidence=None,
        claim_context_text=claim_text,
        claim_context_hash=hashlib.sha256(
            normalize_for_quote_match(claim_text).encode("utf-8")
        ).hexdigest(),
        citation_snapshot=_citation_snapshot(ref.csl_json, citekey),
        created_at=now_iso,
        updated_at=now_iso,
    )
    new_ledger = append_evidence(ledger, row)

    # 9. Atomic commit — proposal.md + citations.json in ONE commit (git
    # first). GitCommitError propagates (web maps it to 500).
    commit_sha = write_and_commit_files(
        repo,
        {
            PROPOSAL_FILENAME: new_doc,
            CITATIONS_FILENAME: dump_ledger(new_ledger),
        },
        f"user insert citation: {citekey} (user:{user_id})",
    )

    # 10. Provenance + resolution rows (committed by the caller, atomically
    # with the mutex release; a DB failure from here on is repaired by
    # reconciliation — same model as save_section step 8).
    session.add(
        ArtifactVersion(
            workspace_id=workspace.id,
            commit_sha=commit_sha,
            created_by="user",
            message=f"user insert citation: {citekey}",
        )
    )
    session.add(
        WorkspaceEvent(
            workspace_id=workspace.id,
            kind="user_citation_insert",
            actor_id=user_id,
            payload={"annotation_id": annotation.id, "ev_id": ev_id, "citekey": citekey},
        )
    )
    annotation.status = "inserted"
    annotation.resolved_by_user_id = user_id
    annotation.resolved_at = datetime.now(UTC)

    # Citekey cache mirror (agent-path parity, _insert_citation_locked step
    # 14): keep references.citekey consistent for user-inserted citekeys.
    # Unlike the agent path (own session) this rides the CALLER's commit —
    # best-effort in the same sense: if that commit fails, the git ledger is
    # canonical and reconciliation backfills the cache.
    if ref.citekey != citekey:
        ref.citekey = citekey

    return {"ev_id": ev_id, "citekey": citekey, "commit_sha": commit_sha}
