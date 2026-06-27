"""Member-gated manual coherence-pass endpoints. POST enqueues (create-or-return-
active); GET latest is polled by the SPA. Cloud-LLM gates mirror the interactive
turns route (web/workspaces.py:779-814) with the SAME status codes/order."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import CoherenceJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.coherence.jobs import (
    enqueue_coherence_run,
    latest_coherence_job,
)
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm
from call_manager.proposal_assist.web._deps import _session_dep
from call_manager.proposal_assist.web._perms import require_member

router = APIRouter(prefix="/api/workspaces", tags=["coherence"])


def _serialize(job: CoherenceJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "n_proposals": job.n_proposals,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _gate_cloud_llm(*, ws: Workspace, request: Request, session: Session, user) -> None:
    gate_cloud_llm(
        ws=ws, request=request, session=session, user=user,
        disclosure_action="starting a coherence pass",
    )


@router.post("/{workspace_id}/coherence-runs")
def start_coherence_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    # require_member returns the live (non-soft-deleted) workspace, 404 if missing,
    # 403 for a non-member/stranger.
    #
    # AUTHORITY (B9 defect 3 — DELIBERATE): this cloud fan-out is gated on
    # require_member, NOT require_owner, even though interactive chat turns
    # (submit_turn) are owner-only. Members triggering coherence/evaluator/review
    # passes is an intentional collaboration affordance. It is kept accountable by
    # the actor-attributed coherence_run_triggered WorkspaceEvent written below
    # (B9 defect 4) and bounded by the per-workspace coherence cap + the org-wide
    # global daily token cap, which now UNIONs this table (B9 defect 2). Do NOT
    # narrow this to owner or add a per-actor sub-cap.
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    _gate_cloud_llm(ws=ws, request=request, session=session, user=user)
    # Detect a freshly-created run BEFORE enqueue (which is create-or-return-active)
    # so the trigger event fires ONCE per run, never on the active-first re-POST —
    # mirrors review_run_triggered / uncited_claims_run_triggered.
    had_active = session.scalar(
        select(CoherenceJob.id).where(
            CoherenceJob.workspace_id == workspace_id,
            CoherenceJob.status.in_(("queued", "running")),
        )
    )
    job = enqueue_coherence_run(session, workspace_id=workspace_id, requested_by=user.id)
    if had_active is None:
        session.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="coherence_run_triggered",
                actor_id=user.id,
                payload={"job_id": str(job.id)},
            )
        )
    session.commit()
    return _serialize(job)


@router.get("/{workspace_id}/coherence-runs/latest")
def latest_coherence_run(
    workspace_id: uuid.UUID,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    require_member(workspace_id=workspace_id, user=user, session=session)
    job = latest_coherence_job(session, workspace_id=workspace_id)
    return _serialize(job) if job is not None else None
