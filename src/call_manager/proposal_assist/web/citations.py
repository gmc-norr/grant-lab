"""Web routes for citations — Task 3 (human-facing evidence-ledger view) and
Task 4 (deterministic validation panel).

Permission policy:
- GET  /workspaces/<id>/citations                     owner OR viewer
- POST /workspaces/<id>/citations/validate            owner OR viewer
  (validation is read-only / non-mutating so viewers may run it)

The git ledger (citations.json) is canonical; this view never writes. A
corrupt/unparseable ledger degrades to an error notice rather than a 500.
"""

from __future__ import annotations

import html as _html
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations import reader as citation_reader
from call_manager.proposal_assist.citations.audit import validate_citations_deterministic
from call_manager.proposal_assist.citations.ledger import find_by_id, load_ledger
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME

# Reuse the exact auth + session deps from references.py so the policy stays
# identical (owner-or-viewer read access; non-member → 403).
from call_manager.proposal_assist.web._perms import require_member as _require_owner_or_viewer
from call_manager.proposal_assist.web.references import _session_dep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["citations"])


@router.get("/{workspace_id}/citations", response_class=HTMLResponse)
def citations_panel(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)
    is_owner = ws.owner_id == user.id

    repo_dir = Path(request.app.state.settings.workspaces_dir) / str(workspace_id) / "repo"
    citations_path = repo_dir / CITATIONS_FILENAME

    rows: list[dict] = []
    ledger_error: str | None = None
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        # A corrupt/unsupported ledger is a degraded state, not a 500.
        ledger_error = f"citations.json could not be read: {exc}"
        ledger = None

    if ledger is not None:
        for row in ledger["evidence"]:
            ev_id = row.get("id")
            if not ev_id:
                # A hand-edited row with no id can't be acted on — skip it
                # (mirrors list_citations_impl in tools/citations.py).
                continue
            locator = row.get("locator") or {}
            claim_context = row.get("claim_context") or {}
            rows.append(
                {
                    "ev_id": ev_id,
                    "citekey": row.get("citekey"),
                    "claim": claim_context.get("text"),
                    "evidence_quote": row.get("evidence_quote"),
                    "stance": row.get("stance"),
                    "confidence": row.get("confidence"),
                    "reference_id": row.get("reference_id"),
                    "section_path": locator.get("section_path"),
                }
            )

    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/workspace_citations.html",
        {
            "user": user,
            "workspace": ws,
            "is_owner": is_owner,
            "citations": rows,
            "ledger_error": ledger_error,
        },
    )


@router.post("/{workspace_id}/citations/validate", response_class=HTMLResponse)
def run_validation(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Run the deterministic citation audit and return an HTMX fragment.

    Deterministic only — NO LLM / citation_service call. The semantic judge
    runs automatically inside the drafting agent's validate_citations tool.

    Auth: owner OR viewer (validation is read-only / non-mutating).
    Corrupt ledger → error fragment (not 500).
    Missing proposal.md → treat as empty text (orphan_row for all rows — correct).
    """
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)

    repo_dir = Path(request.app.state.settings.workspaces_dir) / str(workspace_id) / "repo"
    proposals_path = repo_dir / PROPOSAL_FILENAME
    citations_path = repo_dir / CITATIONS_FILENAME

    # Read proposal text — absent file is treated as empty string so orphan_row
    # issues are correctly emitted for any ledger rows (no live markers).
    proposal_text = proposals_path.read_text() if proposals_path.exists() else ""

    # Load the ledger — corrupt JSON degrades to an error fragment, not 500.
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        error_msg = f"citations.json could not be read: {exc}"
        return request.app.state.templates.TemplateResponse(
            request,
            "proposal_assist/partials/citation_audit_results.html",
            {
                "workspace": ws,
                "issues": [],
                "ledger_error": error_msg,
            },
        )

    # Open a fresh session for the audit DB reads (chunk/reference lookups).
    # We use the injected `session` directly — it is a plain SQLAlchemy Session
    # provided by the _session_dep generator, scoped to this request, which is
    # sufficient for the read-only queries in validate_citations_deterministic.
    issues = validate_citations_deterministic(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        workspace_id=workspace_id,
    )

    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/partials/citation_audit_results.html",
        {
            "workspace": ws,
            "issues": issues,
            "ledger_error": None,
        },
    )


# ---------------------------------------------------------------------------
# Task 5 — citation reader drawer fragment
# ---------------------------------------------------------------------------

_MAX_READER_CHARS = 500_000
_WINDOW_RADIUS = 4000


def _source_line(ref: Reference) -> str:
    csl = ref.csl_json or ref.csl_json_extracted or {}
    title = ref.title or csl.get("title") or ref.filename
    bits = [ref.citekey] if ref.citekey else []
    bits.append(str(title))
    return " · ".join(b for b in bits if b)


def _highlight_whole(text: str) -> str:
    return '<mark id="ev-target">' + _html.escape(text) + "</mark>"


def _highlight_window(full_text: str, s: int, e: int, radius: int = _WINDOW_RADIUS) -> str:
    lo = max(0, s - radius)
    hi = min(len(full_text), e + radius)
    return (
        _html.escape(full_text[lo:s])
        + '<mark id="ev-target">'
        + _html.escape(full_text[s:e])
        + "</mark>"
        + _html.escape(full_text[e:hi])
    )


def _reader_body(
    full_text: str | None, chunk: ReferenceChunk | None, quote: str
) -> tuple[str, str | None]:
    """Missing-data matrix + oversized window. Returns (html, note). Everything
    is HTML-escaped except the single injected <mark>."""
    if full_text is None and chunk is None:
        return "", None
    if full_text is None:
        # text.txt absent, chunk present — highlight the chunk text
        return _highlight_whole(chunk.text), "Showing the cited section."
    span = citation_reader.locate_evidence(full_text, chunk.text if chunk else None, quote)
    if span is None:
        if chunk is not None:
            return _highlight_whole(chunk.text), (
                "Showing the cited section — couldn't pinpoint the exact sentence "
                "in the full document."
            )
        return (
            _html.escape(full_text[: 2 * _WINDOW_RADIUS]),
            "Couldn't pinpoint the cited sentence.",
        )
    s, e = span
    if len(full_text) > _MAX_READER_CHARS:
        return _highlight_window(full_text, s, e), "Showing the cited section of a large document."
    return (
        _html.escape(full_text[:s])
        + '<mark id="ev-target">'
        + _html.escape(full_text[s:e])
        + "</mark>"
        + _html.escape(full_text[e:])
    ), None


def _reader_response(request: Request, ws, *, ctx: dict) -> HTMLResponse:
    base: dict = {
        "source_line": None,
        "claim": None,
        "stance": None,
        "confidence": None,
        "index": 0,
        "total": 0,
        "prev_ev": None,
        "next_ev": None,
        "tombstoned": False,
        "no_text": False,
        "note": None,
        "body_html": "",
        "evidence_id": None,
        "error": None,
    }
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/partials/citation_reader.html",
        {"workspace": ws, **base, **ctx},
    )


@router.get("/{workspace_id}/citations/{evidence_id}/reader", response_class=HTMLResponse)
def citation_reader_fragment(
    workspace_id: uuid.UUID,
    evidence_id: str,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)
    repo_dir = Path(request.app.state.settings.workspaces_dir) / str(workspace_id) / "repo"

    citations_path = repo_dir / CITATIONS_FILENAME
    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError:
        return _reader_response(request, ws, ctx={"error": "Citations are unavailable."})

    ev = find_by_id(ledger, evidence_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="no such evidence id")

    try:
        ref_uuid = uuid.UUID(str(ev["reference_id"]))
    except (ValueError, TypeError):
        return _reader_response(request, ws, ctx={"error": "The cited reference is unavailable."})
    ref = session.get(Reference, ref_uuid)
    if ref is None or ref.workspace_id != workspace_id:
        return _reader_response(request, ws, ctx={"error": "The cited reference is unavailable."})

    locator = ev.get("locator") or {}
    chunk = None
    ci = locator.get("chunk_index")
    if ci is not None:
        chunk = session.scalar(
            select(ReferenceChunk).where(
                ReferenceChunk.reference_id == ref.id,
                ReferenceChunk.chunk_index == ci,
                ReferenceChunk.workspace_id == workspace_id,
            )
        )

    storage = ReferenceStorage(request.app.state.settings.workspaces_dir)
    text_path = storage.text_path_for(workspace_id=workspace_id, reference_id=ref.id)
    # Only read text.txt when indexing is complete; otherwise treat as absent.
    text_indexed = ref.text_status == "done"
    full_text: str | None = (
        text_path.read_text(encoding="utf-8") if text_indexed and text_path.exists() else None
    )

    proposal_path = repo_dir / PROPOSAL_FILENAME
    proposal_text = proposal_path.read_text() if proposal_path.exists() else ""
    order = citation_reader.evidence_order_for_reference(proposal_text, ledger, ev["reference_id"])
    total = len(order)
    pos = order.index(evidence_id) if evidence_id in order else -1
    idx = pos + 1 if pos >= 0 else 0
    prev_ev = order[pos - 1] if pos > 0 else None
    next_ev = order[pos + 1] if 0 <= pos < total - 1 else None

    # no_text=True when text hasn't been indexed yet — show "not available" notice
    # regardless of whether we have a chunk.  Chunk fallback is only used when
    # indexing is done but text.txt is missing (e.g. extraction failed or skipped).
    no_text = not text_indexed
    if no_text:
        body_html, note = "", None
    else:
        body_html, note = _reader_body(full_text, chunk, ev.get("evidence_quote") or "")

    ctx = {
        "source_line": _source_line(ref),
        "claim": (ev.get("claim_context") or {}).get("text"),
        "stance": (ev.get("stance") or "").upper(),
        "confidence": (ev.get("confidence") or "").upper(),
        "index": idx,
        "total": total,
        "prev_ev": prev_ev,
        "next_ev": next_ev,
        "tombstoned": ref.tombstoned_at is not None,
        "no_text": no_text,
        "note": note,
        "body_html": body_html,
        "evidence_id": evidence_id,
    }
    return _reader_response(request, ws, ctx=ctx)
