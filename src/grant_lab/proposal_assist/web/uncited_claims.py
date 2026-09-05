"""Member-gated manual uncited-claims endpoints. POST enqueues (create-or-return-active,
429 on cap); GET latest is polled by the SPA (claims returned ONLY when completed — the raw
in-flight shape is never exposed); POST dismiss records a uncited_claim_dismissed event.
All carry response_models (SP2b-3c lesson: bare-dict endpoints hide client/server drift)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import UncitedClaimsJob, Workspace, WorkspaceEvent
from grant_lab.proposal_assist.collab.canonical import current_artifact_sha
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.turns.caps import check_global_daily_token_cap
from grant_lab.proposal_assist.uncited_claims.jobs import (
    enqueue_uncited_claims_run,
    latest_uncited_claims_job,
    uncited_claims_cap_exceeded,
)
from grant_lab.proposal_assist.web._cloud_gate import gate_cloud_llm
from grant_lab.proposal_assist.web._deps import _session_dep
from grant_lab.proposal_assist.web._perms import require_member

router = APIRouter(prefix="/api/workspaces", tags=["uncited-claims"])

_RunStatus = Literal["queued", "running", "completed", "failed"]


class UncitedClaimsRunOut(BaseModel):
    id: str
    status: _RunStatus
    error: str | None
    started_at: str | None
    completed_at: str | None


class ClaimOut(BaseModel):
    claim_text: str
    section_index: int
    reason: str
    claim_hash: str


class UncitedClaimsLatestOut(UncitedClaimsRunOut):
    stale: bool
    # Present ONLY when status == 'completed' (the finalized, hashed, filtered shape);
    # null otherwise so the in-flight raw shape (no claim_hash) is never serialized.
    claims: list[ClaimOut] | None


class OkOut(BaseModel):
    ok: bool


def _serialize_run(job: UncitedClaimsJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _active_job(session: Session, workspace_id: uuid.UUID) -> UncitedClaimsJob | None:
    return session.scalar(
        select(UncitedClaimsJob).where(
            UncitedClaimsJob.workspace_id == workspace_id,
            UncitedClaimsJob.status.in_(("queued", "running")),
        )
    )


@router.post("/{workspace_id}/uncited-claims-runs", response_model=UncitedClaimsRunOut)
def start_uncited_claims_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    # AUTHORITY (B9 defect 3 — DELIBERATE): gated on require_member, NOT
    # require_owner, even though interactive chat turns (submit_turn) are
    # owner-only. Members triggering this cloud fan-out is an intentional
    # collaboration affordance, kept accountable by the actor-attributed
    # uncited_claims_run_triggered WorkspaceEvent below (this route already
    # emitted one — see `created` branch) and bounded by the per-workspace
    # uncited-claims daily cap PLUS the org-wide global daily token cap, which
    # UNIONs uncited_claims_jobs alongside coherence / evaluator / review (B9
    # defect 2). Do NOT narrow to owner or add a per-actor sub-cap.
    ws: Workspace = require_member(workspace_id=workspace_id, user=user, session=session)
    gate_cloud_llm(
        ws=ws,
        request=request,
        session=session,
        user=user,
        disclosure_action="checking for uncited claims",
    )
    cap = check_global_daily_token_cap(session, settings=request.app.state.settings)
    if not cap.allowed:
        raise HTTPException(
            status_code=429,
            detail="The daily AI usage cap has been reached. Try again tomorrow.",
        )
    # active-first: return the in-flight job if any; otherwise cap-gate the NEW run.
    active = _active_job(session, workspace_id)
    if active is not None:
        return _serialize_run(active)
    if uncited_claims_cap_exceeded(
        session, workspace_id=workspace_id, settings=request.app.state.settings
    ):
        raise HTTPException(status_code=429, detail="daily uncited-claims budget reached")
    job, created = enqueue_uncited_claims_run(
        session, workspace_id=workspace_id, requested_by=user.id
    )
    # Trigger event on NEWLY-created runs ONLY (mirrors review_run_triggered). `created`
    # is True only when this call inserted the job, so neither the active-first branch
    # above NOR a concurrent POST that lost the active-unique race (re-reading the winner)
    # can emit a duplicate event for one job.
    if created:
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="uncited_claims_run_triggered",
                actor_id=user.id,
                payload={"job_id": str(job.id)},
            )
        )
    session.commit()
    return _serialize_run(job)


@router.get(
    "/{workspace_id}/uncited-claims-runs/latest",
    response_model=UncitedClaimsLatestOut | None,
)
def latest_uncited_claims_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    require_member(workspace_id=workspace_id, user=user, session=session)
    job = latest_uncited_claims_job(session, workspace_id=workspace_id)
    if job is None:
        return None
    if job.status == "completed":
        repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
        stale = job.evaluated_draft_sha != current_artifact_sha(
                session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
            )
        claims = job.claims or []
    else:
        stale = False
        claims = None
    return {**_serialize_run(job), "stale": stale, "claims": claims}


@router.post("/{workspace_id}/uncited-claims/{claim_hash}/dismiss", response_model=OkOut)
def dismiss_uncited_claim(
    workspace_id: uuid.UUID,
    claim_hash: str,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="uncited_claim_dismissed",
            actor_id=user.id,
            payload={"claim_hash": claim_hash},
        )
    )
    session.commit()
    return {"ok": True}
