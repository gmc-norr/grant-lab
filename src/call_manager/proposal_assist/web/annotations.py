"""Annotation-strip routes (HTMX fragments). Task 11.

Per section, the strip shows the open claim-analysis findings for one section
SNAPSHOT (sha from the rendered fragment) plus the latest analysis-job state:

* ``GET  /{workspace_id}/sections/{n}/annotations?sha=…`` — the strip
  fragment (owner OR viewer; lazy-loaded + polled by section_view.html).
* ``POST /{workspace_id}/annotations/{id}/insert?n=…``   — owner-only
  user citation insert via :func:`insert_from_annotation` (canonical path).
* ``POST /{workspace_id}/annotations/{id}/dismiss?n=…``  — owner-only
  dismissal with a required reason (audited via WorkspaceEvent).
* ``POST /{workspace_id}/sections/{n}/reanalyze``        — owner-only
  re-enqueue of the section's LIVE content.

Mirrors web/sections.py: same ``_session_dep``, same ``_require_owner`` /
``_require_owner_or_viewer`` auth deps, same error mapping (conflict→409,
CommitMutexTimeout→503 BUSY_ERROR_MESSAGE, GitCommitError→500), templates via
``request.app.state.templates``. The POST routes carry ``n`` as a query param
(the strip template threads it through) so their responses can re-render the
SAME strip into ``#annotations-{n}``; the annotation's own
(section_sha, section_occurrence) addresses which findings group to show.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    ClaimAnalysisJob,
    Reference,
    UserClaimAnnotation,
    Workspace,
    WorkspaceEvent,
)
from call_manager.proposal_assist.analysis.insert import InsertConflict, insert_from_annotation
from call_manager.proposal_assist.analysis.queue import (
    enqueue_containing_section,
    enqueue_section_job,
)
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout
from call_manager.proposal_assist.tools.citations import _citation_snapshot
from call_manager.proposal_assist.web._perms import (
    require_member as _require_owner_or_viewer,
)
from call_manager.proposal_assist.web._perms import (
    require_owner as _require_owner,
)
from call_manager.proposal_assist.web.sections import _read_sections, _section_or_404
from call_manager.proposal_assist.web.workspaces import (
    _session_dep,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["annotations"])

_STRIP_PARTIAL = "proposal_assist/partials/annotation_strip.html"

# ClaimAnalysisJob statuses that mean "analysis is still coming".
_JOB_PENDING_STATUSES = ("queued", "running")


def _ref_labels(session: Session, workspace: Workspace, annotations) -> dict[uuid.UUID, str]:
    """Resolve each annotation's reference to a short "Family Year" label via
    the canonical snapshot helper. One query; workspace-scoped (IDOR
    defense-in-depth — a cross-workspace reference renders as the fallback)."""
    ref_ids = {a.reference_id for a in annotations if a.reference_id is not None}
    if not ref_ids:
        return {}
    rows = session.scalars(
        select(Reference).where(
            Reference.workspace_id == workspace.id,
            Reference.id.in_(ref_ids),
        )
    )
    return {r.id: _citation_snapshot(r.csl_json, r.citekey or r.title or r.filename) for r in rows}


def _strip_response(
    request: Request,
    session: Session,
    workspace: Workspace,
    *,
    n: int,
    sha: str,
    occurrence: int,
    is_owner: bool,
) -> HTMLResponse:
    """Render the annotation strip for one (section_sha, occurrence) group,
    plus the latest analysis-job state for that section content."""
    anns = (
        session.query(UserClaimAnnotation)
        .filter(
            UserClaimAnnotation.workspace_id == workspace.id,
            UserClaimAnnotation.section_sha == sha,
            UserClaimAnnotation.section_occurrence == occurrence,
            UserClaimAnnotation.status == "open",
        )
        .order_by(UserClaimAnnotation.sentence_ordinal, UserClaimAnnotation.id)
        .all()
    )
    labels = _ref_labels(session, workspace, anns)
    items = [
        {
            "id": a.id,
            "kind": a.kind,
            "claim_text": a.claim_text,
            "evidence_quote": a.evidence_quote,
            "ref_label": labels.get(a.reference_id, "reference"),
        }
        for a in anns
    ]
    job = (
        session.query(ClaimAnalysisJob)
        .filter(
            ClaimAnalysisJob.workspace_id == workspace.id,
            ClaimAnalysisJob.section_sha == sha,
        )
        .order_by(ClaimAnalysisJob.queued_at.desc(), ClaimAnalysisJob.id.desc())
        .first()
    )
    job_pending = job is not None and job.status in _JOB_PENDING_STATUSES
    job_failed = job is not None and job.status == "failed"
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        _STRIP_PARTIAL,
        {
            "workspace": workspace,
            "n": n,
            "is_owner": is_owner,
            "annotations": items,
            "job_pending": job_pending,
            "job_failed": job_failed,
            "job_error": (job.last_error or "") if job_failed and job is not None else "",
            "poll_url": f"/workspaces/{workspace.id}/sections/{n}/annotations?sha={sha}",
        },
    )


@router.get("/{workspace_id}/sections/{n}/annotations", response_class=HTMLResponse)
def get_section_annotations(
    workspace_id: uuid.UUID,
    n: int,
    sha: str,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Strip fragment for the section snapshot rendered at index ``n``.

    Occurrence identity: among byte-identical duplicate sections, THIS strip
    shows the findings for the occurrence-th duplicate — the count of sections
    with the same sha among indices < n at HEAD.
    """
    ws = _require_owner_or_viewer(workspace_id=workspace_id, user=user, session=session)
    sections = _read_sections(request, workspace_id).sections  # 404 if no proposal.md
    # Mirror _section_or_404: reject out-of-range n so callers get a clean 404
    # rather than a silent empty strip from an off-by-one index.
    if n < 0 or n >= len(sections):
        raise HTTPException(status_code=404, detail="no such section")
    occurrence = sum(1 for s in sections[:n] if s.sha == sha)
    return _strip_response(
        request,
        session,
        ws,
        n=n,
        sha=sha,
        occurrence=occurrence,
        is_owner=ws.owner_id == user.id,
    )


def _annotation_or_404(
    session: Session, workspace: Workspace, annotation_id: int
) -> UserClaimAnnotation:
    ann = session.get(UserClaimAnnotation, annotation_id)
    if ann is None or ann.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="annotation not found")
    return ann


@router.post("/{workspace_id}/annotations/{annotation_id}/insert", response_class=HTMLResponse)
def post_annotation_insert(
    workspace_id: uuid.UUID,
    annotation_id: int,
    n: int,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Insert the suggested citation via the canonical single write path
    (insert_from_annotation), commit, and re-render the strip."""
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    ann = _annotation_or_404(session, ws, annotation_id)
    settings = request.app.state.settings
    # Capture the strip address BEFORE the service runs: a rollback expires
    # the ORM row, and after a successful insert the section content (and so
    # its sha at HEAD) has changed — the annotation's own addressing is what
    # this strip shows.
    sha, occurrence = ann.section_sha, ann.section_occurrence
    try:
        result = insert_from_annotation(
            session=session,
            workspace=ws,
            user_id=user.id,
            annotation=ann,
            workspaces_dir=settings.workspaces_dir,
        )
        session.commit()
    except InsertConflict as e:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(e)) from e
    except CommitMutexTimeout as e:
        session.rollback()
        raise HTTPException(status_code=503, detail=BUSY_ERROR_MESSAGE) from e
    except GitCommitError as e:
        session.rollback()
        logger.exception(
            "user citation insert failed for workspace %s annotation %s",
            workspace_id,
            annotation_id,
        )
        raise HTTPException(status_code=500, detail="insert failed — nothing was written") from e

    # Fix 5 — post-insert re-analysis enqueue: immediately enqueue a claim
    # analysis job for the section that now contains the new marker. The
    # reconciliation tick would self-heal anyway; this is immediacy only.
    # Content-keyed dedup (ON CONFLICT DO NOTHING) makes the enqueue free if
    # the job already exists for this sha. Only enqueue on a genuine insert
    # (not idempotent) and only when we can locate the section.
    if "idempotent" not in result:
        enqueue_containing_section(
            session,
            workspace_id=workspace_id,
            workspaces_dir=settings.workspaces_dir,
            commit_sha=result["commit_sha"],
            ev_id=result["ev_id"],
        )

    return _strip_response(request, session, ws, n=n, sha=sha, occurrence=occurrence, is_owner=True)


@router.post("/{workspace_id}/annotations/{annotation_id}/dismiss", response_class=HTMLResponse)
def post_annotation_dismiss(
    workspace_id: uuid.UUID,
    annotation_id: int,
    n: int,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    reason: str = Form(...),
) -> HTMLResponse:
    """Dismiss an open annotation with a required, audited reason."""
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    ann = _annotation_or_404(session, ws, annotation_id)
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="a reason is required")
    # Guarded UPDATE: serializes against a concurrent insert that may have
    # already flipped status to "inserted" without taking the commit mutex.
    # The WHERE status='open' predicate means updated==0 if the row raced.
    updated = (
        session.query(UserClaimAnnotation)
        .filter(
            UserClaimAnnotation.id == ann.id,
            UserClaimAnnotation.status == "open",
        )
        .update(
            {
                "status": "dismissed",
                "dismissed_reason": reason,
                "resolved_by_user_id": user.id,
                "resolved_at": datetime.now(UTC),
            },
            synchronize_session=False,
        )
    )
    if not updated:
        session.rollback()
        raise HTTPException(status_code=409, detail="annotation is no longer actionable")
    # Capture strip address from the pre-update snapshot (still valid — the
    # section_sha/section_occurrence are immutable after creation).
    sha, occurrence = ann.section_sha, ann.section_occurrence
    session.add(
        WorkspaceEvent(
            workspace_id=ws.id,
            kind="user_claim_dismissed",
            actor_id=user.id,
            payload={"annotation_id": ann.id, "reason": reason},
        )
    )
    session.commit()
    return _strip_response(request, session, ws, n=n, sha=sha, occurrence=occurrence, is_owner=True)


@router.post("/{workspace_id}/sections/{n}/reanalyze", response_class=HTMLResponse)
def post_section_reanalyze(
    workspace_id: uuid.UUID,
    n: int,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Re-enqueue claim analysis for the section's LIVE content at HEAD."""
    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    r = _read_sections(request, workspace_id)
    section = _section_or_404(r.sections, n)
    head = r.head
    enqueue_section_job(
        session,
        workspace_id=ws.id,
        section_index=section.index,
        section_heading=section.heading,
        section_sha=section.sha,
        commit_sha=head,
    )
    session.commit()
    occurrence = sum(1 for s in r.sections[:n] if s.sha == section.sha)
    return _strip_response(
        request,
        session,
        ws,
        n=n,
        sha=section.sha,
        occurrence=occurrence,
        is_owner=True,
    )
