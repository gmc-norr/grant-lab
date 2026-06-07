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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.proposal_assist.citations.audit import validate_citations_deterministic
from call_manager.proposal_assist.citations.bibliography import (
    BuildBibliographyBlocked,
    build_bibliography,
    compose_export,
)
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME

# Reuse the exact auth + session deps from references.py so the policy stays
# identical (owner-or-viewer read access; non-member → 403).
from call_manager.proposal_assist.web.references import (
    _require_owner_or_viewer,
    _session_dep,
)

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


def _safe_title(title: str) -> str:
    """Sanitize a workspace title for a Content-Disposition filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in title)[:80] or "proposal"


@router.get("/{workspace_id}/export-composed.md")
def export_composed(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    """Export the proposal with inline citations rendered + bibliography appended.

    Unlike ``export.md`` (which serves raw proposal.md, ev-comments and all), this
    transforms every ``[@citekey]<!-- ev:id -->`` marker into the workspace's
    citation style (Vancouver ``[N]`` / author-year ``(Family Year)``), strips all
    ev-comments, and appends the rendered bibliography.

    Fail-closed: it builds the bibliography first (commit=False); if any cited
    reference is unapproved/unresolved/tombstoned it returns a 409 listing the
    blocked references rather than serving a half-transformed document.

    Auth: owner OR viewer (read-only — no commit).
    """
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)

    repo_dir = Path(request.app.state.settings.workspaces_dir) / str(ws.id) / "repo"
    proposal_path = repo_dir / PROPOSAL_FILENAME
    citations_path = repo_dir / CITATIONS_FILENAME

    if not proposal_path.exists():
        return HTMLResponse("no proposal.md yet", status_code=404)

    proposal_text = proposal_path.read_text()

    try:
        ledger = load_ledger(citations_path.read_text() if citations_path.exists() else None)
    except ValueError as exc:
        return HTMLResponse(
            f"citations.json could not be read: {_html.escape(str(exc))}", status_code=409
        )

    style = ws.citation_style or "vancouver"

    try:
        # Build the bibliography in-memory (no commit) — this also performs the
        # fail-closed resolution. compose_export re-runs the same resolution and
        # will raise identically, so a successful build guarantees a clean compose.
        bib = build_bibliography(
            proposal_text=proposal_text,
            ledger=ledger,
            session=session,
            workspace_id=ws.id,
            style=style,
            repo_dir=repo_dir,
            commit=False,
        )
        composed = compose_export(
            proposal_text=proposal_text,
            ledger=ledger,
            session=session,
            workspace_id=ws.id,
            style=style,
            bibliography_text=bib["bibliography_md"] or None,
        )
    except BuildBibliographyBlocked as exc:
        rows = "".join(
            f"<li><code>{_html.escape(str(b.get('citekey')))}</code>: "
            f"{_html.escape(str(b.get('reason')))}</li>"
            for b in exc.blocked
        )
        body = (
            "<h1>Export blocked</h1><p>Some cited references are not citable yet. "
            "Approve their metadata (or have the owner waive it) and retry. A reason "
            "of <code>unresolved_marker</code> means the draft contains a bracketed "
            "citation token that doesn't resolve to a real reference — edit it out of "
            "the proposal text.</p>"
            f"<ul>{rows}</ul>"
        )
        return HTMLResponse(body, status_code=409)

    filename = f"{_safe_title(ws.title)}-composed.md"
    return Response(
        composed,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
