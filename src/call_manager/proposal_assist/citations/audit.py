"""Deterministic citation validation (Slice 3 Task 3).

Pure DB/string re-checks of the canonical citations.json ledger against the
LIVE proposal.md + reference chunks. NO LLM here — the semantic judge is Task 4.

`validate_citations_deterministic` re-derives, for every citation, exactly the
same content hashes that `insert_citation` recorded, so a clean (unmodified)
citation yields ZERO drift issues. The hash bases live in `chunk_content_sha`
and `claim_context_sha` below so Task 4 can reuse the identical byte-exact basis.

The function is defensive: it uses `.get()` everywhere, is workspace-scoped, and
NEVER raises — a single malformed row is isolated so it can't abort the whole
validation. Each issue is a dict:

    {"type": str,
     "severity": "error" | "warning",
     "citekey": str | None,
     "evidence_id": str | None,
     "location": str | None,
     "detail": str}

Issue types emitted:

- ``orphan_marker``      (error)   — inline marker has no backing ledger row
- ``orphan_row``         (error)   — ledger row has no live inline marker
- ``reference_missing``  (error)   — cited reference not found in this workspace
- ``metadata_unapproved``(warning) — reference metadata status is not approved
- ``chunk_missing``      (error)   — chunk index not found for the reference
- ``source_replaced``    (error)   — reference source file changed since citing
- ``index_drift``        (error)   — chunk text changed since the citation was created
- ``quote_missing``      (error)   — evidence quote absent or unlocatable in chunk
- ``claim_drift``        (error)   — claim sentence around the marker changed, or
                                     ``claim_context.hash`` absent (cannot verify)
- ``internal``           (error)   — unexpected exception during per-row processing;
                                     the audit logs the traceback and continues
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.claims import claim_sentence_around
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
    normalize_for_quote_match,
)

logger = logging.getLogger(__name__)

# Reference metadata states a citation may safely rely on.
_APPROVED_METADATA = {"approved", "verified_external"}


def chunk_content_sha(chunk_text: str) -> str:
    """Bare 64-hex sha256 of the raw chunk text.

    MUST match insert_citation's basis (``tools/citations.py`` step 8):
    ``hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()``.
    """
    return hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()


def claim_context_sha(claim_text: str) -> str:
    """Bare 64-hex sha256 of the normalized claim sentence.

    MUST match insert_citation's basis (``tools/citations.py`` step 9):
    ``hashlib.sha256(normalize_for_quote_match(text).encode("utf-8")).hexdigest()``.
    """
    return hashlib.sha256(normalize_for_quote_match(claim_text).encode("utf-8")).hexdigest()


def evidence_quote_sha(quote: str) -> str:
    """Bare 64-hex sha256 of the normalized evidence quote.

    A verdict-cache key component (Task 4): changing the cited quote within an
    unchanged chunk must invalidate the cached verdict (spec D19). Shares the
    same normalize-then-sha256 basis as ``claim_context_sha`` so the three
    normalized-hash components of the cache key cannot drift apart.
    """
    return hashlib.sha256(normalize_for_quote_match(quote).encode("utf-8")).hexdigest()


def _issue(
    type_: str,
    severity: str,
    *,
    citekey: str | None,
    evidence_id: str | None,
    location: str | None,
    detail: str,
) -> dict:
    return {
        "type": type_,
        "severity": severity,
        "citekey": citekey,
        "evidence_id": evidence_id,
        "location": location,
        "detail": detail,
    }


def validate_citations_deterministic(
    *,
    proposal_text: str,
    ledger: dict,
    session,
    workspace_id,
) -> list[dict]:
    """Return a list of issues found by re-checking the ledger against live state.

    Never raises. Workspace-scoped: references/chunks belonging to another
    workspace are treated as missing.
    """
    issues: list[dict] = []

    try:
        markers = find_markers(proposal_text or "")
    except Exception:  # pragma: no cover - find_markers is pure/total
        logger.exception("find_markers failed during citation audit")
        markers = []
    # If a marker ev_id repeats (shouldn't — ev ids are unique), keep the first.
    marker_by_ev: dict[str, dict] = {}
    for m in markers:
        ev = m.get("ev_id")
        if ev and ev not in marker_by_ev:
            marker_by_ev[ev] = m

    evidence = (ledger or {}).get("evidence") or []
    rows_by_ev: dict[str, dict] = {}
    for r in evidence:
        if isinstance(r, dict) and r.get("id"):
            rows_by_ev.setdefault(r["id"], r)

    # 1. orphan_marker — a marker with no backing ledger row.
    for ev_id, m in marker_by_ev.items():
        if ev_id not in rows_by_ev:
            issues.append(
                _issue(
                    "orphan_marker",
                    "error",
                    citekey=m.get("citekey"),
                    evidence_id=ev_id,
                    location=f"char {m.get('start')}",
                    detail="inline citation marker has no matching ledger row",
                )
            )

    # 2. orphan_row — a ledger row with no live marker.
    for ev_id, row in rows_by_ev.items():
        if ev_id not in marker_by_ev:
            issues.append(
                _issue(
                    "orphan_row",
                    "error",
                    citekey=row.get("citekey"),
                    evidence_id=ev_id,
                    location=f"citekey {row.get('citekey')}",
                    detail="ledger row has no inline marker in proposal.md",
                )
            )

    # 3. Per-row checks, only for rows that have a LIVE marker.
    for ev_id, row in rows_by_ev.items():
        marker = marker_by_ev.get(ev_id)
        if marker is None:
            continue  # orphan_row, already flagged; cannot compute claim_drift.
        try:
            issues.extend(
                _check_live_row(
                    row=row,
                    marker=marker,
                    proposal_text=proposal_text or "",
                    session=session,
                    workspace_id=workspace_id,
                )
            )
        except Exception as exc:  # never let one bad row abort the whole audit
            logger.exception("citation audit row %s failed", ev_id)
            issues.append(
                _issue(
                    "internal",
                    "error",
                    citekey=row.get("citekey"),
                    evidence_id=ev_id,
                    location=None,
                    detail=f"internal error validating citation: {exc}",
                )
            )

    return issues


def _check_live_row(
    *,
    row: dict,
    marker: dict,
    proposal_text: str,
    session,
    workspace_id,
) -> list[dict]:
    """Checks for a single ledger row that has a live marker. May raise; the
    caller isolates it."""
    issues: list[dict] = []
    ev_id = row.get("id")
    citekey = row.get("citekey")
    locator = row.get("locator") or {}
    chunk_index = locator.get("chunk_index")
    location = f"citekey {citekey} ({ev_id}) at char {marker.get('start')}"

    # --- reference resolution (workspace-scoped) -------------------------- #
    ref_id_raw = row.get("reference_id")
    try:
        ref_uuid = uuid.UUID(str(ref_id_raw))
    except (ValueError, TypeError):
        issues.append(
            _issue(
                "reference_missing",
                "error",
                citekey=citekey,
                evidence_id=ev_id,
                location=location,
                detail=f"reference_id {ref_id_raw!r} is not a valid uuid",
            )
        )
        return issues

    ref = session.get(Reference, ref_uuid)
    if ref is None or getattr(ref, "workspace_id", None) != workspace_id:
        issues.append(
            _issue(
                "reference_missing",
                "error",
                citekey=citekey,
                evidence_id=ev_id,
                location=location,
                detail="cited reference does not exist in this workspace",
            )
        )
        return issues  # can't proceed without the reference

    # --- metadata_unapproved (warning) ----------------------------------- #
    if getattr(ref, "metadata_status", None) not in _APPROVED_METADATA:
        issues.append(
            _issue(
                "metadata_unapproved",
                "warning",
                citekey=citekey,
                evidence_id=ev_id,
                location=location,
                detail=(
                    "cited reference metadata is not approved "
                    f"(status={getattr(ref, 'metadata_status', None)!r})"
                ),
            )
        )

    # --- chunk load (workspace-scoped) ----------------------------------- #
    chunk = (
        session.query(ReferenceChunk)
        .filter(
            ReferenceChunk.reference_id == ref_uuid,
            ReferenceChunk.chunk_index == chunk_index,
            ReferenceChunk.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if chunk is None:
        issues.append(
            _issue(
                "chunk_missing",
                "error",
                citekey=citekey,
                evidence_id=ev_id,
                location=f"chunk {chunk_index}",
                detail=f"chunk index {chunk_index!r} not found for cited reference",
            )
        )
        return issues  # can't run hash/quote checks without the chunk

    # --- source_replaced (only when BOTH hashes present and differ) ------- #
    row_src = row.get("source_content_hash")
    ref_src = getattr(ref, "original_sha256", None)
    if row_src is not None and ref_src is not None and row_src != ref_src:
        issues.append(
            _issue(
                "source_replaced",
                "error",
                citekey=citekey,
                evidence_id=ev_id,
                location=location,
                detail="reference source file changed since the citation was created",
            )
        )

    # --- index_drift (chunk text changed under the locator) -------------- #
    if chunk_content_sha(chunk.text) != row.get("chunk_content_hash"):
        issues.append(
            _issue(
                "index_drift",
                "error",
                citekey=citekey,
                evidence_id=ev_id,
                location=f"chunk {chunk_index}",
                detail="chunk text changed since the citation was created",
            )
        )

    # --- quote_missing (evidence quote no longer locatable) -------------- #
    quote = row.get("evidence_quote")
    if not quote:
        issues.append(
            _issue(
                "quote_missing",
                "error",
                citekey=citekey,
                evidence_id=ev_id,
                location=f"chunk {chunk_index}",
                detail="evidence quote is empty",
            )
        )
    else:
        try:
            find_quote_offsets(chunk.text, quote)
        except QuoteNotFound:
            issues.append(
                _issue(
                    "quote_missing",
                    "error",
                    citekey=citekey,
                    evidence_id=ev_id,
                    location=f"chunk {chunk_index}",
                    detail="evidence quote not found in chunk",
                )
            )
        except QuoteAmbiguous:
            issues.append(
                _issue(
                    "quote_missing",
                    "error",
                    citekey=citekey,
                    evidence_id=ev_id,
                    location=f"chunk {chunk_index}",
                    detail="evidence quote occurs multiple times in chunk",
                )
            )

    # --- claim_drift (prose around the marker changed) ------------------- #
    live_claim = claim_sentence_around(proposal_text, marker.get("start", 0), marker.get("end", 0))
    stored_hash = (row.get("claim_context") or {}).get("hash")
    if stored_hash is None:
        issues.append(
            _issue(
                "claim_drift",
                "error",
                citekey=citekey,
                evidence_id=ev_id,
                location=location,
                detail="claim_context.hash absent; cannot verify the claim snapshot",
            )
        )
    elif claim_context_sha(live_claim) != stored_hash:
        issues.append(
            _issue(
                "claim_drift",
                "error",
                citekey=citekey,
                evidence_id=ev_id,
                location=location,
                detail="the claim sentence around the marker changed since citing",
            )
        )

    return issues
