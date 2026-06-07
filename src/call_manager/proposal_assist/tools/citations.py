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
        if (
            row.get("reference_id") == reference_id
            and (row.get("locator") or {}).get("chunk_index") == chunk_index
            and row.get("evidence_quote") == evidence_quote
            and row.get("id") in live_ev_ids
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
