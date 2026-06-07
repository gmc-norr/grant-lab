"""The atomic insert_citation tool (Slice 2 Task 5 — the keystone).

insert_citation inserts an inline citation marker into proposal.md AND appends
an evidence row into citations.json in ONE git commit, then best-effort updates
the references.citekey cache. The git ledger is canonical; the DB citekey is a
cache that reconciliation backfills (git-first, cache best-effort — mirrors
proposal.py's contract).

The whole operation runs under the per-workspace advisory lock so a concurrent
edit can't interleave between the quote-verify / marker-build / commit steps.

Tools return a plain dict; {"error": "..."} signals failure to the registry's
_wrap, which the SDK turns into an MCP error envelope. The functions never
raise to the caller for expected failure modes.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    write_and_commit_files,
)
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    dump_ledger,
    find_by_id,
    load_ledger,
    new_ev_id,
    remove_evidence,
    update_evidence,
)
from call_manager.proposal_assist.citations.markers import (
    AnchorError,
    find_markers,
    insert_marker,
    remove_marker,
)
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
    normalize_for_quote_match,
)
from call_manager.proposal_assist.drafting.locking import (
    LockNotAcquired,
    workspace_lock_session,
)
from call_manager.proposal_assist.references.csl import generate_citekey
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from call_manager.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

CITATIONS_FILENAME = "citations.json"

# Spec line 121: stance ∈ {supports, contradicts, background}.
ALLOWED_STANCES = frozenset({"supports", "contradicts", "background"})
# Spec line 122: confidence ∈ {high, medium, low} (agent self-assessment).
ALLOWED_CONFIDENCE = frozenset({"high", "medium", "low"})


def _current_head(repo_dir) -> str:
    """Return the current HEAD sha, or "" if it can't be resolved."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


def _citation_snapshot(csl: dict | None, citekey: str) -> str:
    """Build a short, deterministic human label from CSL (no network).

    Prefers "<first-author-family> <year>"; falls back to the citekey.
    """
    if not isinstance(csl, dict):
        return citekey
    family: str | None = None
    try:
        authors = csl.get("author")
        if authors and isinstance(authors, list) and isinstance(authors[0], dict):
            first = authors[0]
            if first.get("family"):
                family = str(first["family"])
            elif first.get("literal"):
                words = str(first["literal"]).split()
                if words:
                    family = words[-1]
    except Exception:  # noqa: BLE001 — snapshot is best-effort/deterministic only
        family = None

    year: str | None = None
    try:
        issued = csl.get("issued")
        if isinstance(issued, dict):
            date_parts = issued.get("date-parts")
            if (
                date_parts
                and isinstance(date_parts, list)
                and date_parts[0]
                and isinstance(date_parts[0], list)
                and date_parts[0][0] is not None
            ):
                year = str(date_parts[0][0])
    except Exception:  # noqa: BLE001
        year = None

    if family and year:
        return f"{family} {year}"
    if family:
        return family
    return citekey


def _find_idempotent_row(
    ledger: dict,
    *,
    reference_id: str,
    chunk_index: int,
    evidence_quote: str,
    proposal_text: str,
) -> dict | None:
    """Return an existing evidence row identical on (reference_id, chunk_index,
    evidence_quote) whose marker is still present in proposal.md, else None."""
    live_ev_ids = {m["ev_id"] for m in find_markers(proposal_text)}
    for row in ledger["evidence"]:
        # A hand-edited ledger row may be missing 'id'; require a present, truthy
        # id so the return site can subscript it safely (no KeyError on bad rows).
        row_id = row.get("id")
        if (
            row_id
            and row.get("reference_id") == reference_id
            and (row.get("locator") or {}).get("chunk_index") == chunk_index
            and row.get("evidence_quote") == evidence_quote
            and row_id in live_ev_ids
        ):
            return row
    return None


async def insert_citation_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str = "supports",
    confidence: str | None = None,
) -> dict[str, Any]:
    """Insert an inline citation marker + evidence row atomically.

    See the module docstring + the Task 5 spec for the full contract. Returns
    {"ev_id", "citekey", "commit_sha"} on success (plus "idempotent": True when
    a duplicate insert was a no-op), or {"error": "..."} on any expected
    failure (leaving NO commit and a clean working tree).
    """
    if stance not in ALLOWED_STANCES:
        return {"error": f"invalid stance {stance!r}; allowed: {sorted(ALLOWED_STANCES)}"}
    if confidence is not None and confidence not in ALLOWED_CONFIDENCE:
        allowed = sorted(ALLOWED_CONFIDENCE)
        return {"error": f"invalid confidence {confidence!r}; allowed: {allowed}"}

    try:
        ref_uuid = uuid.UUID(str(reference_id))
    except (ValueError, AttributeError, TypeError):
        return {"error": "invalid reference_id (not a uuid)"}

    try:
        with workspace_lock_session(ctx.session_factory, ctx.workspace_id) as session:
            return _insert_citation_locked(
                ctx,
                session,
                ref_uuid=ref_uuid,
                anchor=anchor,
                chunk_index=chunk_index,
                evidence_quote=evidence_quote,
                stance=stance,
                confidence=confidence,
            )
    except LockNotAcquired:
        return {"error": "workspace is busy with another edit; retry"}


def _insert_citation_locked(
    ctx: ToolContext,
    session: Session,
    *,
    ref_uuid: uuid.UUID,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str,
    confidence: str | None,
) -> dict[str, Any]:
    # 1. Load + workspace-scope the Reference.
    ref = session.get(Reference, ref_uuid)
    if ref is None or ref.workspace_id != ctx.workspace_id:
        return {"error": "reference not found"}

    # 2. Load the ReferenceChunk (reference + index), workspace-scoped.
    chunk = (
        session.query(ReferenceChunk)
        .filter(
            ReferenceChunk.reference_id == ref_uuid,
            ReferenceChunk.chunk_index == chunk_index,
            ReferenceChunk.workspace_id == ctx.workspace_id,
        )
        .one_or_none()
    )
    if chunk is None:
        return {"error": "chunk not found"}

    # 3. Read proposal.md + the (possibly absent) ledger.
    proposal_path = ctx.repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
    proposal_text = proposal_path.read_text()

    citations_path = ctx.repo_dir / CITATIONS_FILENAME
    ledger_text = citations_path.read_text() if citations_path.exists() else None
    try:
        ledger = load_ledger(ledger_text)
    except ValueError as exc:
        return {"error": f"citations.json is invalid: {exc}"}

    # 4. Quote-verify against the raw chunk text.
    try:
        start, end = find_quote_offsets(chunk.text, evidence_quote)
    except QuoteNotFound:
        return {"error": "evidence quote not found in chunk"}
    except QuoteAmbiguous:
        return {"error": "evidence quote is ambiguous (occurs multiple times) in chunk"}

    # 5. Idempotency — identical (ref, chunk, quote) with a live marker → no-op.
    existing = _find_idempotent_row(
        ledger,
        reference_id=str(ref_uuid),
        chunk_index=chunk_index,
        evidence_quote=evidence_quote,
        proposal_text=proposal_text,
    )
    if existing is not None:
        # commit_sha on the idempotent path is the current HEAD (informational —
        # no new commit was made) and may be "" if HEAD resolution fails.
        # existing["id"] is safe: _find_idempotent_row only matches rows with a
        # present, truthy id.
        return {
            "ev_id": existing["id"],
            "citekey": existing.get("citekey"),
            "commit_sha": _current_head(ctx.repo_dir),
            "idempotent": True,
        }

    # 6. Citekey — reuse the ref's if set, else generate; guard against ledger lag.
    if ref.citekey:
        citekey = ref.citekey
    else:
        citekey = generate_citekey(
            ref.csl_json or ref.csl_json_extracted or {},
            ctx.workspace_id,
            session,
        )
    for row in ledger["evidence"]:
        if row.get("citekey") == citekey and row.get("reference_id") != str(ref_uuid):
            return {"error": "citekey collision detected; ledger/DB out of sync — reconcile"}

    # 7. Build the new proposal text (validates anchor uniqueness; nothing written yet).
    ev_id = new_ev_id()
    try:
        new_proposal_text = insert_marker(proposal_text, anchor, citekey, ev_id)
    except AnchorError as exc:
        if "ambiguous" in str(exc):
            return {"error": "anchor is ambiguous"}
        return {"error": "anchor not found"}

    # 8. Hashes — BARE 64-hex, matching references.original_sha256's column format.
    chunk_content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    source_content_hash = ref.original_sha256  # may be None

    # 9. Claim context — the anchor sentence snapshot + normalized hash.
    claim_context_text = anchor
    claim_context_hash = hashlib.sha256(
        normalize_for_quote_match(anchor).encode("utf-8")
    ).hexdigest()

    # 10. Citation snapshot (deterministic, no network).
    snapshot = _citation_snapshot(ref.csl_json, citekey)

    # 11. Timestamp.
    now = datetime.now(UTC).isoformat()

    # 12. Build the row + new ledger.
    row = build_evidence_row(
        ev_id=ev_id,
        citekey=citekey,
        reference_id=str(ref_uuid),
        chunk_index=chunk_index,
        section_path=chunk.section_path,
        source_content_hash=source_content_hash,
        chunk_content_hash=chunk_content_hash,
        evidence_quote=evidence_quote,
        evidence_quote_offsets=[start, end],
        stance=stance,
        confidence=confidence,
        claim_context_text=claim_context_text,
        claim_context_hash=claim_context_hash,
        citation_snapshot=snapshot,
        created_at=now,
        updated_at=now,
    )
    new_ledger = append_evidence(ledger, row)

    # 13. Atomic commit — proposal.md + citations.json in ONE commit (git first).
    try:
        sha = write_and_commit_files(
            ctx.repo_dir,
            {
                PROPOSAL_FILENAME: new_proposal_text,
                CITATIONS_FILENAME: dump_ledger(new_ledger),
            },
            f"cite: add {citekey} [{ev_id}]",
        )
    except GitCommitError:
        logger.exception(
            "insert_citation: atomic commit failed for workspace %s (ev %s)",
            ctx.workspace_id,
            ev_id,
        )
        return {"error": "failed to commit citation"}

    # 14. DB cache (AFTER git) — best-effort; git ledger is canonical.
    if ref.citekey != citekey:
        try:
            ref.citekey = citekey
            session.commit()
        except Exception:  # noqa: BLE001 — cache write must never fail the tool
            session.rollback()
            logger.warning(
                "insert_citation: references.citekey cache update failed for "
                "workspace %s ref %s citekey %s — reconciliation will backfill",
                ctx.workspace_id,
                ref_uuid,
                citekey,
                exc_info=True,
            )

    # 15. Success.
    return {"ev_id": ev_id, "citekey": citekey, "commit_sha": sha}


# --- remove_citation -------------------------------------------------------


async def remove_citation_impl(
    ctx: ToolContext,
    *,
    evidence_id: str,
) -> dict[str, Any]:
    """Remove one citation occurrence (its marker + ledger row) atomically.

    Mirrors insert_citation: git-first, whole-op under the workspace lock.
    Returns {"ev_id", "commit_sha"} on success, or {"error": "..."} on any
    expected failure (leaving NO commit and a clean working tree). Removal is
    per-ev-id — other markers/rows sharing the same citekey are untouched, and
    references.citekey is intentionally NOT changed here (other occurrences may
    still use it; tombstoning is out of scope for remove).
    """
    try:
        with workspace_lock_session(ctx.session_factory, ctx.workspace_id):
            return _remove_citation_locked(ctx, evidence_id=evidence_id)
    except LockNotAcquired:
        return {"error": "workspace is busy with another edit; retry"}


def _remove_citation_locked(ctx: ToolContext, *, evidence_id: str) -> dict[str, Any]:
    # 1. Read proposal.md + the ledger.
    proposal_path = ctx.repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
    proposal_text = proposal_path.read_text()

    citations_path = ctx.repo_dir / CITATIONS_FILENAME
    ledger_text = citations_path.read_text() if citations_path.exists() else None
    try:
        ledger = load_ledger(ledger_text)
    except ValueError as exc:
        return {"error": f"citations.json is invalid: {exc}"}

    # 2. Locate the row (the ledger is canonical).
    row = find_by_id(ledger, evidence_id)
    if row is None:
        return {"error": "citation not found"}

    # 3. Drop the marker (per-ev-id) and the row.
    new_proposal_text, removed_marker = remove_marker(proposal_text, evidence_id)
    new_ledger, removed_row = remove_evidence(ledger, evidence_id)
    if not removed_marker and not removed_row:
        return {"error": "citation not found"}
    if removed_row and not removed_marker:
        logger.warning(
            "remove_citation: ledger row %s had no matching marker in %s "
            "(workspace %s) — dropping the row anyway; ledger is canonical",
            evidence_id,
            PROPOSAL_FILENAME,
            ctx.workspace_id,
        )

    # 4. Atomic commit — proposal.md + citations.json in ONE commit (git first).
    try:
        sha = write_and_commit_files(
            ctx.repo_dir,
            {
                PROPOSAL_FILENAME: new_proposal_text,
                CITATIONS_FILENAME: dump_ledger(new_ledger),
            },
            f"cite: remove {row.get('citekey')} [{evidence_id}]",
        )
    except GitCommitError:
        logger.exception(
            "remove_citation: atomic commit failed for workspace %s (ev %s)",
            ctx.workspace_id,
            evidence_id,
        )
        return {"error": "failed to commit citation removal"}

    # 5. Success.
    return {"ev_id": evidence_id, "commit_sha": sha}


# --- update_citation -------------------------------------------------------


def _claim_sentence_around(text: str, marker_start: int, marker_end: int) -> str:
    """Return the live claim text around a marker for reaccept_claim.

    Snapshots the sentence containing the marker: from the previous sentence
    terminator (. ! ? or newline) up to and including the next one, with the
    marker span itself excised so the stored claim is the prose, not the marker.
    Falls back to the marker's line, then to the whole text, if no boundary is
    found. Result is whitespace-collapsed.
    """
    prose = text[:marker_start] + text[marker_end:]
    # The marker is gone from `prose`; the claim now centres on marker_start.
    pos = marker_start
    terminators = ".!?\n"
    start = 0
    for i in range(pos - 1, -1, -1):
        if prose[i] in terminators:
            start = i + 1
            break
    end = len(prose)
    for i in range(pos, len(prose)):
        if prose[i] in terminators:
            end = i + 1
            break
    sentence = prose[start:end]
    return normalize_for_quote_match(sentence)


async def update_citation_impl(
    ctx: ToolContext,
    *,
    evidence_id: str,
    chunk_index: int | None = None,
    stance: str | None = None,
    confidence: str | None = None,
    evidence_quote: str | None = None,
    reaccept_claim: bool = False,
) -> dict[str, Any]:
    """Update one citation row atomically (re-pinning chunk/quote re-verifies).

    Mirrors insert_citation: git-first, whole-op under the workspace lock. The
    marker text in proposal.md is unchanged (the citekey never changes here), so
    only citations.json is normally committed. Returns
    {"ev_id", "commit_sha", "updated_fields"} on success, or {"error": "..."}.
    A call that specifies no actual change returns {"error": "no changes
    specified"} BEFORE writing (no empty commits).
    """
    if stance is not None and stance not in ALLOWED_STANCES:
        return {"error": f"invalid stance {stance!r}; allowed: {sorted(ALLOWED_STANCES)}"}
    if confidence is not None and confidence not in ALLOWED_CONFIDENCE:
        allowed = sorted(ALLOWED_CONFIDENCE)
        return {"error": f"invalid confidence {confidence!r}; allowed: {allowed}"}

    if (
        chunk_index is None
        and stance is None
        and confidence is None
        and evidence_quote is None
        and not reaccept_claim
    ):
        return {"error": "no changes specified"}

    try:
        with workspace_lock_session(ctx.session_factory, ctx.workspace_id) as session:
            return _update_citation_locked(
                ctx,
                session,
                evidence_id=evidence_id,
                chunk_index=chunk_index,
                stance=stance,
                confidence=confidence,
                evidence_quote=evidence_quote,
                reaccept_claim=reaccept_claim,
            )
    except LockNotAcquired:
        return {"error": "workspace is busy with another edit; retry"}


def _update_citation_locked(
    ctx: ToolContext,
    session: Session,
    *,
    evidence_id: str,
    chunk_index: int | None,
    stance: str | None,
    confidence: str | None,
    evidence_quote: str | None,
    reaccept_claim: bool,
) -> dict[str, Any]:
    # 1. Read proposal.md + the ledger; locate the row.
    proposal_path = ctx.repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
    proposal_text = proposal_path.read_text()

    citations_path = ctx.repo_dir / CITATIONS_FILENAME
    ledger_text = citations_path.read_text() if citations_path.exists() else None
    try:
        ledger = load_ledger(ledger_text)
    except ValueError as exc:
        return {"error": f"citations.json is invalid: {exc}"}

    row = find_by_id(ledger, evidence_id)
    if row is None:
        return {"error": "citation not found"}

    fields: dict[str, Any] = {}

    # 2. Scalar field updates.
    if stance is not None:
        fields["stance"] = stance
    if confidence is not None:
        fields["confidence"] = confidence

    # 3. Re-verify the evidence binding when chunk_index or quote changes.
    if chunk_index is not None or evidence_quote is not None:
        locator = row.get("locator") or {}
        new_chunk_index = chunk_index if chunk_index is not None else locator.get("chunk_index")
        new_quote = evidence_quote if evidence_quote is not None else row.get("evidence_quote")

        try:
            ref_uuid = uuid.UUID(str(row.get("reference_id")))
        except (ValueError, AttributeError, TypeError):
            return {"error": "invalid reference_id (not a uuid)"}

        chunk = (
            session.query(ReferenceChunk)
            .filter(
                ReferenceChunk.reference_id == ref_uuid,
                ReferenceChunk.chunk_index == new_chunk_index,
                ReferenceChunk.workspace_id == ctx.workspace_id,
            )
            .one_or_none()
        )
        if chunk is None:
            return {"error": "chunk not found"}

        try:
            start, end = find_quote_offsets(chunk.text, new_quote)
        except QuoteNotFound:
            return {"error": "evidence quote not found in chunk"}
        except QuoteAmbiguous:
            return {"error": "evidence quote is ambiguous (occurs multiple times) in chunk"}

        fields["evidence_quote"] = new_quote
        fields["evidence_quote_offsets"] = [start, end]
        fields["chunk_content_hash"] = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        fields["locator"] = {
            "chunk_index": new_chunk_index,
            "section_path": chunk.section_path,
        }

    # 4. reaccept_claim — re-snapshot the live claim around the marker.
    if reaccept_claim:
        marker = next((m for m in find_markers(proposal_text) if m["ev_id"] == evidence_id), None)
        if marker is not None:
            claim_text = _claim_sentence_around(proposal_text, marker["start"], marker["end"])
        else:
            # No live marker (e.g. orphaned row) — fall back to the stored claim
            # text so we still refresh the hash deterministically.
            claim_text = (row.get("claim_context") or {}).get("text", "")
        fields["claim_context"] = {
            "text": claim_text,
            "hash": hashlib.sha256(
                normalize_for_quote_match(claim_text).encode("utf-8")
            ).hexdigest(),
        }
        # Clear any drift flag a validator may have set (out-of-scope field, but
        # reaccept is the documented way to clear it — be defensive).
        if "claim_drift" in row:
            fields["claim_drift"] = None

    # 5. Nothing actually changed (e.g. only invalid no-op combos slipped through).
    if not fields:
        return {"error": "no changes specified"}

    # 6. Always refresh updated_at on a real change.
    fields["updated_at"] = datetime.now(UTC).isoformat()

    new_ledger, updated = update_evidence(ledger, evidence_id, **fields)
    if not updated:
        return {"error": "citation not found"}

    # 7. Atomic commit — only citations.json changes (marker/citekey unchanged).
    try:
        sha = write_and_commit_files(
            ctx.repo_dir,
            {CITATIONS_FILENAME: dump_ledger(new_ledger)},
            f"cite: update [{evidence_id}]",
        )
    except GitCommitError:
        logger.exception(
            "update_citation: atomic commit failed for workspace %s (ev %s)",
            ctx.workspace_id,
            evidence_id,
        )
        return {"error": "failed to commit citation update"}

    # 8. Success.
    return {
        "ev_id": evidence_id,
        "commit_sha": sha,
        "updated_fields": sorted(fields.keys()),
    }
