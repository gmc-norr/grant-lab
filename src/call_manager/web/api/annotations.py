"""Claim-analysis "Claims" tab /api surface. Re-exposes the existing annotation
workflow (read findings, insert, dismiss, reanalyze) as typed JSON for the React
cockpit, at parity with the doomed Jinja web/annotations.py strip — but
self-contained on KEPT primitives only (no import of the doomed Jinja helpers).
No new business logic, no schema change."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    ClaimAnalysisJob,
    Reference,
    User,
    UserClaimAnnotation,
)
from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.tools.citations import _citation_snapshot
from call_manager.proposal_assist.web._perms import require_member

router = APIRouter()

# ClaimAnalysisJob statuses that mean "analysis is still coming".
_JOB_PENDING_STATUSES = ("queued", "running")


class ClaimFindingOut(BaseModel):
    id: int
    kind: str
    claim_text: str
    evidence_quote: str | None
    ref_label: str
    can_insert: bool
    sentence_ordinal: int


class ClaimJobOut(BaseModel):
    status: str | None  # "pending" | "failed" | None (done / no job)
    error: str | None


class ClaimSectionOut(BaseModel):
    index: int
    heading: str
    section_sha: str
    occurrence: int
    job: ClaimJobOut
    findings: list[ClaimFindingOut]


class AnnotationsOut(BaseModel):
    is_owner: bool
    sections: list[ClaimSectionOut]


@router.get("/workspaces/{workspace_id}/annotations", response_model=AnnotationsOut)
def get_annotations(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> AnnotationsOut:
    """Claims-tab read-model: every live draft section with its open findings +
    the latest analysis-job state. Degrades to {is_owner, sections: []} (200) on
    ANY pre-draft state — no repo / no HEAD / no proposal.md — because the Claims
    tab is always mounted (a strip fragment may 404; an always-present tab cannot)."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        is_owner = ws.owner_id == user.id
        repo = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        proposal = repo / PROPOSAL_FILENAME
        if not proposal.exists():
            return AnnotationsOut(is_owner=is_owner, sections=[])
        try:
            current_head(repo)  # validate the repo has a commit; raises on pre-draft
        except GitCommitError:
            return AnnotationsOut(is_owner=is_owner, sections=[])
        sections = split_sections(proposal.read_text())

        # One query for all open annotations; group by (sha, occurrence).
        anns = list(
            session.scalars(
                select(UserClaimAnnotation)
                .where(
                    UserClaimAnnotation.workspace_id == ws.id,
                    UserClaimAnnotation.status == "open",
                )
                .order_by(UserClaimAnnotation.sentence_ordinal, UserClaimAnnotation.id)
            )
        )
        by_group: dict[tuple[str, int], list[UserClaimAnnotation]] = {}
        for a in anns:
            by_group.setdefault((a.section_sha, a.section_occurrence), []).append(a)

        # One query for "Family Year" ref labels (workspace-scoped — a
        # cross-workspace reference renders as the fallback).
        ref_ids = {a.reference_id for a in anns if a.reference_id is not None}
        labels: dict[uuid.UUID, str] = {}
        if ref_ids:
            for r in session.scalars(
                select(Reference).where(
                    Reference.workspace_id == ws.id,
                    Reference.id.in_(ref_ids),
                )
            ):
                labels[r.id] = _citation_snapshot(r.csl_json, r.citekey or r.title or r.filename)

        # One query for jobs; keep the latest per section_sha.
        latest_job: dict[str, ClaimAnalysisJob] = {}
        for j in session.scalars(
            select(ClaimAnalysisJob)
            .where(ClaimAnalysisJob.workspace_id == ws.id)
            .order_by(ClaimAnalysisJob.queued_at.desc(), ClaimAnalysisJob.id.desc())
        ):
            latest_job.setdefault(j.section_sha, j)

        out_sections: list[ClaimSectionOut] = []
        for sec in sections:
            # occurrence = count of earlier sections with the same sha (mirrors
            # the proven Jinja get_section_annotations mapping).
            occurrence = sum(1 for s in sections[: sec.index] if s.sha == sec.sha)
            group = by_group.get((sec.sha, occurrence), [])
            findings = [
                ClaimFindingOut(
                    id=a.id,
                    kind=a.kind,
                    claim_text=a.claim_text,
                    evidence_quote=a.evidence_quote,
                    ref_label=labels.get(a.reference_id, "reference"),
                    can_insert=(a.kind == "uncited_supported"),
                    sentence_ordinal=a.sentence_ordinal,
                )
                for a in group
            ]
            job = latest_job.get(sec.sha)
            if job is not None and job.status in _JOB_PENDING_STATUSES:
                job_out = ClaimJobOut(status="pending", error=None)
            elif job is not None and job.status == "failed":
                job_out = ClaimJobOut(status="failed", error=job.last_error or "")
            else:
                job_out = ClaimJobOut(status=None, error=None)
            out_sections.append(
                ClaimSectionOut(
                    index=sec.index,
                    heading=sec.heading,
                    section_sha=sec.sha,
                    occurrence=occurrence,
                    job=job_out,
                    findings=findings,
                )
            )
    return AnnotationsOut(is_owner=is_owner, sections=out_sections)
