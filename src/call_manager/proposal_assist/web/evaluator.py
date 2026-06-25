"""Member-gated manual evaluator endpoints. POST enqueues (create-or-return-active);
GET latest is polled by the SPA; GET scorecard returns the latest completed job's
scorecard plus server-computed staleness (draft / spec / rubric)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.evaluator.context import content_sha
from call_manager.proposal_assist.evaluator.jobs import (
    enqueue_evaluator_run,
    latest_completed_evaluator_job,
    latest_evaluator_job,
)
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web._deps import _session_dep

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
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    _gate_cloud_llm(ws=ws, request=request, session=session, user=user)
    job = enqueue_evaluator_run(session, workspace_id=workspace_id, requested_by=user.id)
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
    if job.evaluated_draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME):
        reasons.append("draft")
    if job.evaluated_spec_sha != content_sha(repo_dir / SPEC_FILENAME):
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
