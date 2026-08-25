"""User-initiated citation insert from an annotation. Canonical path ONLY
(spec §4): evidence ledger row + [@citekey]<!-- ev:id --> marker, one atomic
commit of proposal.md + citations.json. Occurrence-aware idempotency: no-op
only when THIS annotation is already inserted (same evidence live elsewhere
does NOT satisfy it — spec rev-3 finding 4).

Caller owns the transaction: provenance rows are added but NOT committed
here, and the commit mutex is released by the caller's COMMIT/ROLLBACK
(mirrors editing/save.py). A DB failure AFTER the git commit is recovered
by reconciliation (same atomicity model as save_section).

3b-5d.D4: the core (derive_annotation_cite + compute_annotation_insert) is
extracted so the collab kernel path (apply_annotation_insert_to_room,
collab/kernel.py) can drive the SAME occurrence-aware compute against the
live room instead of git — WHY a bespoke core rather than reusing the generic
compute_citation_insert: this insert is occurrence-aware (the same
(reference_id, chunk_index, evidence_quote) live ELSEWHERE in the document
does NOT satisfy an insert targeted at THIS annotation's occurrence — see
compute_annotation_insert's docstring), so the generic
compute_citation_insert's ledger-wide _find_idempotent_row scan would falsely
short-circuit it, and its evidence row lacks the claim_context_text/hash this
core stamps on every row. Extracting a bespoke derive/compute pair keeps that
narrower repair semantics intact on both the git AND the kernel path.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
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

# Intra-package private imports — house precedent: tools/audit.py imports
# _read_proposal_and_ledger from citations.core the same way. These are the
# canonical agent-insert pieces; reusing them keeps ONE citekey resolution and
# ONE "Family Year" snapshot for both the agent and the user insert paths.
from call_manager.proposal_assist.citations.core import (
    _citation_snapshot,
    _resolve_citekey_for_ref,
)
from call_manager.proposal_assist.citations.gitio import write_and_commit_files
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    dump_ledger,
    load_ledger,
    new_ev_id,
)
from call_manager.proposal_assist.citations.markers import (
    AnchorError,
    find_markers,
    insert_marker,
)
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
    normalize_for_quote_match,
)
from call_manager.proposal_assist.collab.state import raise_if_collab_severed
from call_manager.proposal_assist.editing.save import (
    PROPOSAL_FILENAME,
    TURN_IN_FLIGHT_MESSAGE,
    _turn_in_flight,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.fencing import acquire_commit_mutex_at_generation
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.ports.stamp import stamp_for_workspace
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME

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


@dataclass(frozen=True)
class DerivedCite:
    section_index: int
    anchor: str
    reference_id: uuid.UUID
    chunk_index: int
    evidence_quote: str
    quote_start: int
    quote_end: int


@dataclass(frozen=True)
class AnnotationInsertOutcome:
    """Either a claim-context repair hit (``repair`` set — the annotation's
    status/resolved fields are ALREADY mutated by the time this is returned,
    and NOTHING new was written), or a fresh insert (``new_doc``/``new_ledger``/
    ``ev_id``/``citekey`` set — nothing committed yet, that is the caller's job)."""

    repair: tuple[str, str | None] | None = None
    new_doc: str | None = None
    new_ledger: dict | None = None
    ev_id: str | None = None
    citekey: str | None = None


def derive_annotation_cite(
    session: Session, *, workspace: Workspace, annotation: UserClaimAnnotation, doc: str
) -> DerivedCite | None:
    """Steps 3-6 of insert_from_annotation, extracted VERBATIM with the document
    text parameterized (git path passes the disk read; the kernel path passes
    the live-room serialization). Returns None when THIS annotation is already
    inserted (the idempotent signal); raises InsertConflict exactly as before.
    Contains NO mutex/expire/turn-lock — callers own those."""
    # 3. Occurrence-aware idempotency (spec rev-3 finding 4): only THIS
    # annotation being inserted is a no-op. The same (ref, chunk, quote)
    # being live elsewhere in the document does NOT satisfy this occurrence,
    # so there is deliberately NO _find_idempotent_row-style ledger scan here.
    if annotation.status == "inserted":
        return None
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
        new_ext = max(0, ext - _ANCHOR_EXTEND_STEP)
        if new_ext == ext:
            # Already at the leftmost boundary — further iterations would
            # produce an identical anchor, so break immediately.
            break
        ext = new_ext
        anchor = target.text[ext:end].rstrip("\n")
        tries += 1
    if doc.count(anchor) != 1:
        raise InsertConflict("repeated text — cite it via chat or edit the sentence to be unique")

    return DerivedCite(
        section_index=target.index,
        anchor=anchor,
        reference_id=ref.id,
        chunk_index=annotation.chunk_index,
        evidence_quote=annotation.evidence_quote,
        quote_start=q_start,
        quote_end=q_end,
    )


def compute_annotation_insert(
    session: Session,
    *,
    workspace: Workspace,
    annotation: UserClaimAnnotation,
    user_id: int,
    doc: str,
    ledger: dict,
    derived: DerivedCite,
) -> AnnotationInsertOutcome:
    """Steps 6.5-8 of insert_from_annotation, extracted VERBATIM (parameterized
    on doc/ledger/derived — the caller already ran derive_annotation_cite and
    passes its result through so this does not re-walk the anchor search).
    Writes NO git; the caller (git path or kernel path) owns the commit.

    Occurrence-aware by construction: the claim-context-hash-keyed repair scan
    below is the ONLY idempotency check here — it repairs a partial prior
    write (git commit landed, DB commit rolled back) for THIS exact
    (reference, chunk, quote, claim-context) tuple. It deliberately does NOT
    do a generic _find_idempotent_row-style scan (tools/citations.py) — the
    same (reference_id, chunk_index, evidence_quote) already cited at ANOTHER
    sentence must NOT satisfy this occurrence (spec rev-3 finding 4)."""
    ref = session.get(Reference, derived.reference_id)
    chunk = (
        session.query(ReferenceChunk)
        .filter(
            ReferenceChunk.reference_id == derived.reference_id,
            ReferenceChunk.chunk_index == derived.chunk_index,
            ReferenceChunk.workspace_id == workspace.id,
        )
        .one_or_none()
    )
    anchor = derived.anchor
    q_start, q_end = derived.quote_start, derived.quote_end

    # Idempotency repair for a partial prior write: the git commit (step 9)
    # lands BEFORE annotation.status='inserted' is set on the session and
    # committed by the CALLER. If that caller commit fails (e.g. a citekey
    # unique-index race or any OperationalError), the git marker+ledger row are
    # already durable but annotation.status rolls back to 'open'. A naive retry
    # would mint a fresh ev_id and write a SECOND marker for the same evidence.
    # Detect that prior row — occurrence-aware: same (reference_id, chunk_index,
    # evidence_quote) AND the same claim-context hash AND a still-live marker —
    # and repair (flip status + return idempotent) instead of re-inserting.
    # Keying on claim_context_hash keeps this from collapsing two genuinely
    # distinct citations of the same quote for different claims (the case the
    # plain status-only check at the top deliberately left to occurrence
    # semantics); a true same-quote/same-claim retry is the only thing matched.
    claim_ctx_hash = hashlib.sha256(
        normalize_for_quote_match(annotation.claim_text).encode("utf-8")
    ).hexdigest()
    live_ev_ids = {m["ev_id"] for m in find_markers(doc)}
    for r in ledger.get("evidence", []):
        rid = r.get("id")
        if (
            rid
            and rid in live_ev_ids
            and r.get("reference_id") == str(ref.id)
            and (r.get("locator") or {}).get("chunk_index") == annotation.chunk_index
            and r.get("evidence_quote") == annotation.evidence_quote
            and (r.get("claim_context") or {}).get("hash") == claim_ctx_hash
        ):
            annotation.status = "inserted"
            annotation.resolved_by_user_id = user_id
            annotation.resolved_at = datetime.now(UTC)
            return AnnotationInsertOutcome(repair=(rid, r.get("citekey")))

    ev_id = new_ev_id()
    citekey = _resolve_citekey_for_ref(session, ledger=ledger, ref=ref, workspace_id=workspace.id)
    try:
        new_doc = insert_marker(doc, anchor, citekey, ev_id)
    except AnchorError as exc:
        # The pre-check (doc.count(anchor) == 1) already passed, so the only
        # way insert_marker raises AnchorError here is an existing marker
        # overlapping the anchor (e.g. the anchor runs through a citekey
        # substring). Both ambiguity and not-found map to the same friendly
        # message because the user cannot distinguish the underlying cause.
        raise InsertConflict(
            "repeated text — cite it via chat or edit the sentence to be unique"
        ) from exc

    # Evidence row via the single authoritative constructor — identical
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

    return AnnotationInsertOutcome(
        new_doc=new_doc, new_ledger=new_ledger, ev_id=ev_id, citekey=citekey
    )


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
    ``CommitMutexTimeout`` (web maps to 429), ``GitCommitError`` (500), and —
    both via app-level handlers in ``main.py``, both as 404 — ``WorkspaceDeleted``
    and ``WorkspaceGenerationStale``. The latter is Primitive B's addition: the
    workspace is live, but at a LATER epoch than the caller's stamp, i.e. it was
    deleted and restored while this request was in flight.
    """
    # 1. Serialize against every other git mutation of this workspace, and refuse
    # outright if it was deleted — or restored into a new epoch — while we
    # blocked. `stamp_for_workspace` mints the belief from the object
    # `require_owner` loaded at the ACCESS CHECK, before the mutex could block;
    # re-reading the generation here would compare the post-race value against
    # itself and fence nothing. All three propagate — the web layer maps
    # CommitMutexTimeout to 429, and main.py maps WorkspaceDeleted and
    # WorkspaceGenerationStale to the same 404.
    # Flag-off-only path: with collab enabled the step-1b severance recheck below raises
    # CollabSeveranceConflict, which the route maps to 409, so this path is retiring rather than
    # maintained. Slice C3 bounds its tail anyway — the 2s cap now left in force by
    # acquire_commit_mutex applies at every site, including this one, with nothing needed here.
    stamp = stamp_for_workspace(workspace)
    acquire_commit_mutex_at_generation(session, stamp.workspace_id, generation=stamp.generation)

    # 1b. Severance recheck UNDER the mutex (3b-5d.D4 Item 2b): enable-on-touch may
    # have flipped draft collab on while we blocked on the mutex, after the route's
    # pre-mutex guard passed. Abort writing NOTHING; the route maps
    # CollabSeveranceConflict to its 409. No-op when the flag is off / kill-switch.
    raise_if_collab_severed(session, workspace_id=workspace.id, artifact="draft")

    # Expire annotation immediately after acquiring the mutex: SQLAlchemy's
    # identity map still holds the pre-block snapshot. A concurrent insert
    # that committed while we were blocked would have flipped annotation.status
    # to "inserted", but the ORM would not see it without re-SELECTing. Under
    # READ COMMITTED the next attribute access re-issues a SELECT, so the
    # idempotency check below is authoritative inside the mutex.
    session.expire(annotation)

    # 2. Turn-lock: an agent turn in flight owns the repo.
    if _turn_in_flight(session, workspace.id):
        raise InsertConflict(TURN_IN_FLIGHT_MESSAGE)

    repo = workspace_repo_dir(workspaces_dir, workspace.id)
    proposal_path = repo / PROPOSAL_FILENAME
    if not proposal_path.exists():
        raise InsertConflict("the text changed — re-analysis is pending")
    doc = proposal_path.read_text()

    derived = derive_annotation_cite(session, workspace=workspace, annotation=annotation, doc=doc)
    if derived is None:
        return {"idempotent": True}

    # 7. Ledger + citekey (same resolution as the agent path) + marker.
    citations_path = repo / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        logger.error("corrupt citations.json in workspace %s: %s", workspace.id, exc)
        raise InsertConflict(f"citations.json is invalid: {exc}") from exc

    outcome = compute_annotation_insert(
        session,
        workspace=workspace,
        annotation=annotation,
        user_id=user_id,
        doc=doc,
        ledger=ledger,
        derived=derived,
    )
    if outcome.repair is not None:
        ev_id, citekey = outcome.repair
        return {"idempotent": True, "ev_id": ev_id, "citekey": citekey}

    # 9. Atomic commit — proposal.md + citations.json in ONE commit (git
    # first). GitCommitError propagates (web maps it to 500).
    commit_sha = write_and_commit_files(
        repo,
        {
            PROPOSAL_FILENAME: outcome.new_doc,
            CITATIONS_FILENAME: dump_ledger(outcome.new_ledger),
        },
        f"user insert citation: {outcome.citekey} (user:{user_id})",
    )

    # 10. Provenance + resolution rows (committed by the caller, atomically
    # with the mutex release; a DB failure from here on is repaired by
    # reconciliation — same model as save_section step 8).
    session.add(
        ArtifactVersion(
            workspace_id=workspace.id,
            commit_sha=commit_sha,
            created_by="user",
            message=f"user insert citation: {outcome.citekey}",
        )
    )
    session.add(
        WorkspaceEvent(
            workspace_id=workspace.id,
            kind="user_citation_insert",
            actor_id=user_id,
            payload={
                "annotation_id": annotation.id,
                "ev_id": outcome.ev_id,
                "citekey": outcome.citekey,
            },
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
    ref = session.get(Reference, derived.reference_id)
    if ref.citekey != outcome.citekey:
        ref.citekey = outcome.citekey

    return {"ev_id": outcome.ev_id, "citekey": outcome.citekey, "commit_sha": commit_sha}
