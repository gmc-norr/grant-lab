"""Member-gated on-demand mock-panel endpoints. POST enqueues (active-first, then
cap-check); GET latest is polled by the SPA; GET files/{name} returns a review file
at the run's stored blob SHA."""

from __future__ import annotations

import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ReviewRun, ReviewRunItem, WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations.gitio import GitCommitError, read_blob
from call_manager.proposal_assist.evaluator.context import content_sha
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.review.jobs import (
    active_review_run,
    enqueue_review_run,
    latest_review_run,
    review_dollar_cap,
    review_dollars_today,
    review_run_cap,
    review_runs_today,
)
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm
from call_manager.proposal_assist.web._deps import _session_dep
from call_manager.proposal_assist.web._perms import require_member

router = APIRouter(prefix="/api/workspaces", tags=["review"])

_FILES = {"fit-check.md": "fit_check_sha", "rubric-review.md": "rubric_review_sha"}
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


# ---------------------------------------------------------------------------
# Response models. These pin the wire contract into the OpenAPI schema so the
# generated TS client is type-checked against it (the prior bare-``dict`` return
# emitted no response schema, which is what hid the dollar-string crash). The
# three Numeric dollar columns are serialized as JSON *strings* (see `str(...)`
# in `_serialize`/`_budget`); declaring them `str` keeps the wire bytes
# identical while making "string, not number" explicit to codegen. The enums
# mirror the DB CHECK constraints verbatim — note item status carries `skipped`
# (an item-only state) and run status carries `partial` (a run-only state).
# ---------------------------------------------------------------------------

ReviewItemKind = Literal[
    "fit_check",
    "persona_domain_skeptic",
    "persona_methodology_critic",
    "persona_translation_skeptic",
    "persona_devils_advocate",
    "consolidation",
]
ReviewItemStatus = Literal["queued", "running", "done", "failed", "skipped", "cancelled"]
ReviewRunStatus = Literal["queued", "running", "done", "failed", "cancelled", "partial"]


class ReviewRunItemOut(BaseModel):
    kind: ReviewItemKind
    status: ReviewItemStatus
    error_message: str | None
    estimated_dollars: str | None


class ReviewBudgetOut(BaseModel):
    runs_used: int
    runs_cap: int
    dollars_used: str
    dollars_cap: str


class ReviewRunOut(BaseModel):
    id: str
    status: ReviewRunStatus
    triggered_at: str | None
    completed_at: str | None
    draft_sha: str
    fit_check_sha: str | None
    rubric_review_sha: str | None
    estimated_dollars: str | None
    items: list[ReviewRunItemOut]
    stale: bool
    budget: ReviewBudgetOut


class ReviewFileOut(BaseModel):
    name: str
    sha: str
    markdown: str


def _serialize(run: ReviewRun, items: list[ReviewRunItem], *, stale: bool, budget: dict) -> dict:
    return {
        "id": str(run.id),
        "status": run.status,
        "triggered_at": run.triggered_at.isoformat() if run.triggered_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "draft_sha": run.draft_sha,
        "fit_check_sha": run.fit_check_sha,
        "rubric_review_sha": run.rubric_review_sha,
        "estimated_dollars": (
            str(run.estimated_dollars) if run.estimated_dollars is not None else None
        ),
        "items": [
            {
                "kind": i.kind,
                "status": i.status,
                "error_message": i.error_message,
                "estimated_dollars": (
                    str(i.estimated_dollars) if i.estimated_dollars is not None else None
                ),
            }
            for i in sorted(items, key=lambda x: x.kind)
        ],
        "stale": stale,
        "budget": budget,
    }


def _budget(session, ws, settings, *, workspace_id) -> dict:
    runs_cap = review_run_cap(ws, settings)
    dollars_cap = review_dollar_cap(ws, settings)
    return {
        "runs_used": review_runs_today(session, workspace_id=workspace_id),
        "runs_cap": runs_cap,
        "dollars_used": str(review_dollars_today(session, workspace_id=workspace_id)),
        "dollars_cap": str(dollars_cap),
    }


@router.post("/{workspace_id}/review-runs", response_model=ReviewRunOut)
def start_review_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    gate_cloud_llm(ws=ws, request=request, session=session, user=user,
                   disclosure_action="running a mock-panel review")
    settings = request.app.state.settings
    # 1. active-first (create-or-return-active; no cap-check on the existing run)
    active = active_review_run(session, workspace_id=workspace_id)
    if active is None:
        # 2. cap-check only when creating a NEW run
        runs_hit = (
            review_runs_today(session, workspace_id=workspace_id)
            >= review_run_cap(ws, settings)
        )
        dollars_hit = (
            review_dollars_today(session, workspace_id=workspace_id)
            >= review_dollar_cap(ws, settings)
        )
        if runs_hit or dollars_hit:
            raise HTTPException(
                status_code=429,
                detail="Daily mock-panel review budget reached for this workspace.",
            )
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        draft_sha = content_sha(repo_dir / PROPOSAL_FILENAME)
        run = enqueue_review_run(session, workspace_id=workspace_id, draft_sha=draft_sha)
        session.add(WorkspaceEvent(
            workspace_id=workspace_id, kind="review_run_triggered", actor_id=user.id, payload={},
        ))
        session.commit()
    else:
        run = active
    items = session.query(ReviewRunItem).filter_by(review_run_id=run.id).all()
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
    stale = run.draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME)
    return _serialize(
        run, items, stale=stale, budget=_budget(session, ws, settings, workspace_id=workspace_id)
    )


@router.get("/{workspace_id}/review-runs/latest", response_model=ReviewRunOut | None)
def latest_review(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    run = latest_review_run(session, workspace_id=workspace_id)
    if run is None:
        return None
    settings = request.app.state.settings
    items = session.query(ReviewRunItem).filter_by(review_run_id=run.id).all()
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
    stale = run.draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME)
    return _serialize(
        run, items, stale=stale, budget=_budget(session, ws, settings, workspace_id=workspace_id)
    )


@router.get(
    "/{workspace_id}/review-runs/{run_id}/files/{name}",
    response_model=ReviewFileOut,
)
def review_file(
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    name: str,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    sha_attr = _FILES.get(name)
    if sha_attr is None:
        raise HTTPException(status_code=404, detail="unknown review file")
    run = session.get(ReviewRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="review run not found")
    sha = getattr(run, sha_attr)
    if not sha or not _SHA_RE.match(sha):
        raise HTTPException(status_code=404, detail="file not produced for this run")
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    try:
        markdown = read_blob(repo_dir, sha)
    except GitCommitError:
        raise HTTPException(status_code=404, detail="file content unavailable") from None
    return {"name": name, "sha": sha, "markdown": markdown}
