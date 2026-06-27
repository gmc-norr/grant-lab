"""JSON /api citation audit (Cockpit Slice F).

GET  …/citation-audit       member-gated, deterministic only (free, read-only).
POST …/citation-audit/deep  owner-gated, deterministic + semantic LLM judge,
                            reusing app.state.citation_service (built at startup).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.audit import (
    validate_citations_deterministic,
    validate_citations_semantic_capped,
)
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.usage import check_citation_caps
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.web._perms import require_member, require_owner
from call_manager.web.api.errors import api_error

router = APIRouter()


class AuditIssueOut(BaseModel):
    type: str
    severity: str
    citekey: str | None
    evidence_id: str | None
    location: str | None
    detail: str


class CitationAuditOut(BaseModel):
    draft_present: bool
    issues: list[AuditIssueOut]
    error_count: int
    warning_count: int


class DeepCheckOut(BaseModel):
    semantic_available: bool
    checked: int
    deferred: int
    cap_exceeded: bool
    error_count: int
    warning_count: int
    issues: list[AuditIssueOut]


def _counts(issues: list[dict]) -> tuple[int, int]:
    err = sum(1 for i in issues if i["severity"] == "error")
    warn = sum(1 for i in issues if i["severity"] == "warning")
    return err, warn


def _load(repo_dir):
    """Return (proposal_text, ledger) or None if there is no draft. Raises ValueError
    on a corrupt citations.json."""
    proposal = repo_dir / PROPOSAL_FILENAME
    if not proposal.exists():
        return None
    citations = repo_dir / CITATIONS_FILENAME
    ledger = load_ledger(citations.read_text() if citations.exists() else None)
    return proposal.read_text(), ledger


@router.get("/workspaces/{workspace_id}/citation-audit", response_model=CitationAuditOut)
def citation_audit(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        try:
            loaded = _load(repo_dir)
        except ValueError as exc:
            return api_error(
                409, "citation_ledger_invalid", f"citations.json could not be read: {exc}"
            )
        if loaded is None:
            return CitationAuditOut(draft_present=False, issues=[], error_count=0, warning_count=0)
        proposal_text, ledger = loaded
        issues = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=session, workspace_id=workspace_id
        )
        err, warn = _counts(issues)
        return CitationAuditOut(
            draft_present=True, issues=issues, error_count=err, warning_count=warn
        )


@router.post("/workspaces/{workspace_id}/citation-audit/deep", response_model=DeepCheckOut)
def citation_audit_deep(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    citation_service = request.app.state.citation_service
    semantic_available = citation_service is not None
    with sf() as session:
        require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        try:
            loaded = _load(repo_dir)
        except ValueError as exc:
            return api_error(
                409, "citation_ledger_invalid", f"citations.json could not be read: {exc}"
            )
        if loaded is None:
            return DeepCheckOut(
                semantic_available=semantic_available,
                checked=0,
                deferred=0,
                cap_exceeded=False,
                error_count=0,
                warning_count=0,
                issues=[],
            )
        proposal_text, ledger = loaded
        det = validate_citations_deterministic(
            proposal_text=proposal_text, ledger=ledger, session=session, workspace_id=workspace_id
        )
        if semantic_available:
            max_fresh = settings.citation_deep_check_max_per_request
            sem, checked, deferred = validate_citations_semantic_capped(
                proposal_text=proposal_text,
                ledger=ledger,
                session=session,
                session_factory=sf,
                workspace_id=workspace_id,
                citation_service=citation_service,
                deterministic_issues=det,
                max_fresh_calls=max_fresh,
            )
            # PRECISE cap signal: read the actual daily usage cap. NOT
            # any(audit_unavailable) — the engine emits audit_unavailable for transient
            # errors (audit.py:626) and invalid verdicts (audit.py:660) too, not just
            # cap hits (audit.py:638), which would show a false "AI budget" banner.
            cap_exceeded = check_citation_caps(session, workspace_id, settings=settings).exceeded
        else:
            sem = []
            checked = 0  # spec: checked == 0 when semantic is unavailable
            deferred = 0
            cap_exceeded = False
        issues = det + sem
        err, warn = _counts(issues)
        return DeepCheckOut(
            semantic_available=semantic_available,
            checked=checked,
            deferred=deferred,
            cap_exceeded=cap_exceeded,
            error_count=err,
            warning_count=warn,
            issues=issues,
        )
