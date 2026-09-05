"""Member-gated manual evaluator endpoints. POST enqueues (create-or-return-active);
GET latest is polled by the SPA; GET scorecard returns the latest completed job's
scorecard plus server-computed staleness (draft / spec / rubric)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import EvaluatorJob, Workspace, WorkspaceEvent
from grant_lab.proposal_assist.collab.canonical import current_artifact_sha
from grant_lab.proposal_assist.evaluator.jobs import (
    enqueue_evaluator_run,
    latest_completed_evaluator_job,
    latest_evaluator_job,
)
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.turns.caps import check_global_daily_token_cap
from grant_lab.proposal_assist.web._cloud_gate import gate_cloud_llm
from grant_lab.proposal_assist.web._deps import _session_dep
from grant_lab.proposal_assist.web._perms import require_member

router = APIRouter(prefix="/api/workspaces", tags=["evaluator"])


def _serialize(job: EvaluatorJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _gate_cloud_llm(*, ws: Workspace, request: Request, session: Session, user) -> None:
    gate_cloud_llm(
        ws=ws, request=request, session=session, user=user,
        disclosure_action="running an evaluation",
    )


@router.post("/{workspace_id}/evaluator-runs")
def start_evaluator_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    # AUTHORITY (B9 defect 3 — DELIBERATE): gated on require_member, NOT
    # require_owner, even though interactive chat turns (submit_turn) are
    # owner-only. Members triggering this cloud fan-out is an intentional
    # collaboration affordance, kept accountable by the actor-attributed
    # evaluator_run_triggered WorkspaceEvent below (B9 defect 4) and bounded by
    # the org-wide global daily token cap, which now UNIONs this table (B9 defect
    # 2). Do NOT narrow to owner or add a per-actor sub-cap.
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    _gate_cloud_llm(ws=ws, request=request, session=session, user=user)
    cap = check_global_daily_token_cap(session, settings=request.app.state.settings)
    if not cap.allowed:
        raise HTTPException(
            status_code=429,
            detail="The daily AI usage cap has been reached. Try again tomorrow.",
        )
    # Detect a freshly-created run BEFORE enqueue (create-or-return-active) so the
    # trigger event fires ONCE per run, never on the active-first re-POST.
    had_active = session.scalar(
        select(EvaluatorJob.id).where(
            EvaluatorJob.workspace_id == workspace_id,
            EvaluatorJob.status.in_(("queued", "running")),
        )
    )
    job = enqueue_evaluator_run(session, workspace_id=workspace_id, requested_by=user.id)
    if had_active is None:
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="evaluator_run_triggered",
                actor_id=user.id,
                payload={"job_id": str(job.id)},
            )
        )
    session.commit()
    return _serialize(job)


@router.get("/{workspace_id}/evaluator-runs/latest")
def latest_evaluator_run(
    workspace_id: uuid.UUID,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    require_member(workspace_id=workspace_id, user=user, session=session)
    job = latest_evaluator_job(session, workspace_id=workspace_id)
    return _serialize(job) if job is not None else None


@router.get("/{workspace_id}/scorecard")
def get_scorecard(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    job = latest_completed_evaluator_job(session, workspace_id=workspace_id)
    if job is None:
        return None
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    reasons: list[str] = []
    if job.evaluated_draft_sha != current_artifact_sha(
                session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
            ):
        reasons.append("draft")
    if job.evaluated_spec_sha != current_artifact_sha(
                session, workspace_id=workspace_id, artifact="spec", repo_dir=repo_dir
            ):
        reasons.append("spec")
    if job.evaluated_rubric_updated_at != ws.rubric_updated_at:
        reasons.append("rubric")
    return {
        "scorecard": job.scorecard,
        "evaluated_draft_sha": job.evaluated_draft_sha,
        "evaluated_spec_sha": job.evaluated_spec_sha,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "stale": bool(reasons),
        "stale_reasons": reasons,
    }
