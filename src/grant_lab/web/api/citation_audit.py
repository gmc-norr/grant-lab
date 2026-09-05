"""JSON /api citation audit (Cockpit Slice F).

GET  …/citation-audit       member-gated, deterministic only (free, read-only).
POST …/citation-audit/deep  owner-gated, deterministic + semantic LLM judge,
                            reusing app.state.citation_service (built at startup).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import User
from grant_lab.proposal_assist.citations.audit import (
    validate_citations_deterministic,
    validate_citations_semantic_capped,
)
from grant_lab.proposal_assist.citations.ledger import load_ledger
from grant_lab.proposal_assist.citations.usage import check_citation_caps
from grant_lab.proposal_assist.collab.canonical import current_artifact_text_and_sha
from grant_lab.proposal_assist.editing.save import CITATIONS_FILENAME
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.web._cloud_gate import (
    citation_llm_egresses,
    has_disclosure_ack,
)
from grant_lab.proposal_assist.web._perms import require_member, require_owner
from grant_lab.web.api.errors import api_error

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


def _load(session, workspace_id, repo_dir):
    """Return (proposal_text, ledger) or None if there is no draft. Raises ValueError
    on a corrupt citations.json.

    The draft is the document as it is RIGHT NOW — the room-canonical projection of
    the durable Yjs log when collab is on, raw git HEAD when it is off. Reading git
    unconditionally reported orphan markers the researcher had already deleted, and
    missed the ones they had just written.

    The ledger stays on disk deliberately: citations.json is canonical in git, and the
    collab citation path co-commits it under the same mutex that appends the room
    update, so a marker in the live draft always has its ledger row.

    "No draft" now also covers an EXISTING but empty proposal.md (previously
    draft_present=True with nothing to audit) — the same convention
    current_artifact_text_and_sha uses, where "" means "no artifact".
    """
    proposal_text, _ = current_artifact_text_and_sha(
        session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
    )
    if not proposal_text:
        return None
    citations = repo_dir / CITATIONS_FILENAME
    ledger = load_ledger(citations.read_text() if citations.exists() else None)
    return proposal_text, ledger


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
            loaded = _load(session, workspace_id, repo_dir)
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
        # The deep check IS the cloud call — the user asked for it explicitly, so
        # refuse rather than silently return a weaker answer. No-op under the
        # default on-prem provider. (The reference routes degrade instead: there
        # the LLM only enriches metadata for an upload that is not an AI action.)
        if citation_llm_egresses(settings):
            if not settings.drafting_enabled:
                return api_error(
                    503,
                    "cloud_llm_disabled",
                    "AI features are currently disabled by an administrator.",
                )
            if not has_disclosure_ack(
                session=session, workspace_id=workspace_id, user_id=user.id
            ):
                return api_error(
                    403,
                    "disclosure_not_acknowledged",
                    "Please acknowledge the data-disclosure notice before running "
                    "the deep citation check.",
                )
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        try:
            loaded = _load(session, workspace_id, repo_dir)
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
