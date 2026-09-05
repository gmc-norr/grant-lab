"""Human select-to-cite (SP2a-4b): a direct citation commit initiated by the
workspace owner from the cockpit editor. Mirrors analysis/insert.py
(insert_from_annotation) but takes an explicit anchor + chunk-local quote
offsets instead of a UserClaimAnnotation, applies save_section's section
staleness predicate, and deliberately omits the deleted _turn_in_flight freeze
(concurrency = commit mutex + section staleness). Caller owns the transaction:
provenance rows are added but NOT committed here (mirrors editing/save.py).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from grant_lab.db.models import (
    ArtifactVersion,
    Reference,
    ReferenceChunk,
    Workspace,
    WorkspaceEvent,
)
from grant_lab.proposal_assist.citations import claims
from grant_lab.proposal_assist.citations.core import (
    _citation_snapshot,
    _resolve_citekey_for_ref,
)
from grant_lab.proposal_assist.citations.gitio import current_head, write_and_commit_files
from grant_lab.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    dump_ledger,
    load_ledger,
    new_ev_id,
)
from grant_lab.proposal_assist.citations.markers import (
    AnchorError,
    anchor_occurrences,
    find_markers,
    insert_marker,
)
from grant_lab.proposal_assist.citations.quotes import normalize_for_quote_match
from grant_lab.proposal_assist.collab.state import raise_if_collab_severed
from grant_lab.proposal_assist.editing.save import CITATIONS_FILENAME, PROPOSAL_FILENAME
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.fencing import acquire_commit_mutex_at_generation
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.ports.stamp import stamp_for_workspace

logger = logging.getLogger(__name__)

_ALLOWED_STANCES = frozenset({"supports", "contradicts", "background"})


class CiteError(RuntimeError):
    """Base; str(exc) is user-facing (the endpoint echoes it)."""


class CiteStale(CiteError):
    """Cited section moved, or the anchor is no longer in the saved text (-> 409)."""


class CiteInvalid(CiteError):
    """Bad offsets/quote/chunk, ambiguous anchor, or bad stance (-> 422)."""


class CiteRefNotFound(CiteError):
    """Reference missing / foreign / tombstoned (-> 404)."""


def insert_citation_for_user(
    *,
    session: Session,
    workspace: Workspace,
    user_id: int,
    section_index: int,
    base_commit_sha: str,
    base_section_sha: str,
    anchor: str,
    reference_id: uuid.UUID,
    chunk_index: int,
    evidence_quote: str,
    quote_start: int,
    quote_end: int,
    stance: str = "supports",
    workspaces_dir: Path | str,
) -> dict:
    """Insert a human-selected citation. Returns {ev_id, citekey, commit_sha}.

    Raises CiteStale / CiteInvalid / CiteRefNotFound (mapped by the endpoint to
    409 / 422 / 404), CommitMutexTimeout (429) and GitCommitError (500). Two more
    escape the endpoint's except chain entirely and are answered 404 by app-level
    handlers in main.py: WorkspaceDeleted, and — Primitive B's addition —
    WorkspaceGenerationStale, meaning the workspace is live but at a LATER epoch
    than the caller's stamp (deleted and restored while this request was in
    flight). The caller owns the transaction (this adds provenance rows but does
    not commit)."""
    if stance not in _ALLOWED_STANCES:
        raise CiteInvalid(f"invalid stance {stance!r}")
    if not anchor:
        raise CiteInvalid("select a claim span to cite")

    # 1. Serialize against every other git mutation of this workspace, and refuse
    # outright if it was deleted — or restored into a new epoch — while we blocked.
    # `stamp_for_workspace` mints the belief from the object `require_owner` loaded at
    # the ACCESS CHECK, before the mutex could block; re-reading the generation here
    # would compare the post-race value against itself and fence nothing.
    # Flag-off-only path: with collab enabled the step-1b severance recheck below raises
    # CollabSeveranceConflict, which the route maps to 409, so this path is retiring rather than
    # maintained. Slice C3 bounds its tail anyway — the 2s cap now left in force by
    # acquire_commit_mutex applies at every site, including this one, with nothing needed here.
    stamp = stamp_for_workspace(workspace)
    acquire_commit_mutex_at_generation(session, stamp.workspace_id, generation=stamp.generation)

    # 1b. Severance recheck UNDER the mutex (3b-5d.D4 Item 2b follow-up): the
    # route's _route() fork read the flag as False, but enable-on-touch may have
    # flipped draft collab on while we blocked on the mutex. Abort writing
    # NOTHING; the route maps CollabSeveranceConflict to the same 409 as its
    # severance guard. No-op when the flag is off / kill-switch armed.
    raise_if_collab_severed(session, workspace_id=workspace.id, artifact="draft")

    # 2. Live document under the mutex + save_section's staleness predicate
    #    (HEAD moving is fine as long as THIS section's bytes are unchanged).
    repo = workspace_repo_dir(workspaces_dir, workspace.id)
    proposal_path = repo / PROPOSAL_FILENAME
    if not proposal_path.exists():
        raise CiteStale("this document changed — reopen and re-cite")
    head = current_head(repo)
    doc = proposal_path.read_text()
    sections = split_sections(doc)
    if not 0 <= section_index < len(sections):
        raise CiteStale("this section changed — reopen and re-cite")
    live = sections[section_index]
    if head != base_commit_sha and live.sha != base_section_sha:
        raise CiteStale("this section changed — reopen and re-cite")
    # The anchor MUST belong to the validated section. insert_marker searches the
    # whole document, so without this a stale/forged (section_index,
    # base_section_sha) pair could land the marker in a DIFFERENT, unvalidated
    # section.
    # Marker-overlap-aware: the anchor must occur as PLAIN TEXT (not inside an
    # existing citation marker) within the VALIDATED section. A raw `in` would
    # count a marker-internal occurrence, letting insert_marker's whole-doc search
    # land the marker at the only PLAIN-TEXT occurrence — which could be in a
    # DIFFERENT, unvalidated section. Requiring >=1 plain-text occurrence here,
    # combined with insert_marker's whole-doc uniqueness, guarantees the insert
    # lands in this section (if the doc had a 2nd plain-text occurrence elsewhere,
    # insert_marker raises ambiguous -> CiteInvalid).
    if not anchor_occurrences(live.text, anchor):
        raise CiteStale("this section changed — reopen and re-cite")

    # 3. Reference: workspace-scoped, not tombstoned.
    ref = session.get(Reference, reference_id)
    if ref is None or ref.workspace_id != workspace.id or ref.tombstoned_at is not None:
        raise CiteRefNotFound("that source is no longer available")

    # 4. Chunk (owned ref) + EXACT chunk-local offset verification. The client
    #    sends Unicode code-point offsets; chunk.text is code-point indexed, so
    #    the slice is authoritative and we store it verbatim.
    chunk = (
        session.query(ReferenceChunk)
        .filter(
            ReferenceChunk.reference_id == ref.id,
            ReferenceChunk.chunk_index == chunk_index,
            ReferenceChunk.workspace_id == workspace.id,
        )
        .one_or_none()
    )
    if chunk is None:
        raise CiteInvalid("the selected source passage is unavailable")
    if not (0 <= quote_start < quote_end <= len(chunk.text)):
        raise CiteInvalid("the selected passage is out of range — reselect it")
    sliced = chunk.text[quote_start:quote_end]
    if sliced != evidence_quote:
        raise CiteInvalid("the selected passage changed — reselect it")
    if not sliced.strip():
        raise CiteInvalid("select a non-empty passage")

    # 5. Ledger + citekey + marker. The anchor must be UNIQUE in the whole
    #    proposal; ambiguous -> 422, otherwise (not found) -> 409 (stale).
    citations_path = repo / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        logger.error("corrupt citations.json in workspace %s: %s", workspace.id, exc)
        raise CiteInvalid(f"citations.json is invalid: {exc}") from exc
    ev_id = new_ev_id()
    citekey = _resolve_citekey_for_ref(session, ledger=ledger, ref=ref, workspace_id=workspace.id)
    try:
        new_doc = insert_marker(doc, anchor, citekey, ev_id)
    except AnchorError as exc:
        if "ambiguous" in str(exc):
            raise CiteInvalid(
                "your selected claim appears more than once — select a longer, unique span"
            ) from exc
        raise CiteStale("this section changed — reopen and re-cite") from exc

    # 6. Claim context — the LIVE claim sentence around the just-inserted marker,
    #    via the canonical extractor (claims.py: insert/update/validate MUST share
    #    claim_sentence_around, so a fresh insert never immediately audits as
    #    claim_drift). Fall back to the bare anchor only if extraction degenerates.
    inserted = next((m for m in find_markers(new_doc) if m["ev_id"] == ev_id), None)
    claim_context_text = (
        claims.claim_sentence_around(new_doc, inserted["start"], inserted["end"])
        if inserted is not None
        else ""
    )
    if not claim_context_text:
        claim_context_text = anchor

    # 7. Evidence row (exact stored offsets).
    now_iso = datetime.now(UTC).isoformat()
    row = build_evidence_row(
        ev_id=ev_id,
        citekey=citekey,
        reference_id=str(ref.id),
        chunk_index=chunk_index,
        section_path=chunk.section_path,
        source_content_hash=ref.original_sha256,
        chunk_content_hash=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
        evidence_quote=sliced,
        evidence_quote_offsets=[quote_start, quote_end],
        stance=stance,
        confidence=None,
        claim_context_text=claim_context_text,
        claim_context_hash=hashlib.sha256(
            normalize_for_quote_match(claim_context_text).encode("utf-8")
        ).hexdigest(),
        citation_snapshot=_citation_snapshot(ref.csl_json, citekey),
        created_at=now_iso,
        updated_at=now_iso,
    )
    new_ledger = append_evidence(ledger, row)

    # 8. Atomic commit — proposal.md + citations.json in ONE commit.
    commit_sha = write_and_commit_files(
        repo,
        {PROPOSAL_FILENAME: new_doc, CITATIONS_FILENAME: dump_ledger(new_ledger)},
        f"user select-to-cite: {citekey} (user:{user_id})",
    )

    # 9. Provenance (committed by the caller, atomically with the mutex release).
    session.add(
        ArtifactVersion(
            workspace_id=workspace.id,
            commit_sha=commit_sha,
            created_by="user",
            message=f"user select-to-cite: {citekey}",
        )
    )
    session.add(
        WorkspaceEvent(
            workspace_id=workspace.id,
            kind="user_citation_insert",
            actor_id=user_id,
            payload={"ev_id": ev_id, "citekey": citekey, "section_index": section_index},
        )
    )
    if ref.citekey != citekey:
        ref.citekey = citekey
    return {"ev_id": ev_id, "citekey": citekey, "commit_sha": commit_sha}
