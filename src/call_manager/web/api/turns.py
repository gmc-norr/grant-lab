"""Cockpit chat JSON/SSE turns surface (Slice B). Thin /api layer over the
existing propose-only turn engine: submit reuses submit_user_turn + the shared
cloud gate; the stream is the JSON analogue of the Jinja SSE bridge. No new
business logic, no schema change."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ChatTurn, TurnJob, User, WorkspaceEvent
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout
from call_manager.proposal_assist.turns.caps import GlobalTokenCapExceeded, TokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm
from call_manager.proposal_assist.web._perms import require_member, require_owner

router = APIRouter()

_DISCLOSURE_ACTION = "sending a message to the drafting assistant"


class TurnOut(BaseModel):
    id: str
    role: str
    content: Any  # JSONB: str | {type:text,...} | list[block]
    created_at: str
    position: int


class SubmitTurnIn(BaseModel):
    content: str
    reference_ids: list[str] = []


class SubmitTurnResponse(BaseModel):
    job_id: str
    user_turn: TurnOut


def _turn_to_dict(turn: ChatTurn) -> dict:
    return {
        "id": str(turn.id),
        "role": turn.role,
        "content": turn.content,
        "created_at": turn.created_at.isoformat(),
        "position": turn.position,
    }


@router.post("/workspaces/{workspace_id}/turns", response_model=SubmitTurnResponse)
def submit_turn(
    workspace_id: uuid.UUID,
    body: SubmitTurnIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SubmitTurnResponse:
    """Enqueue a user turn (owner-only). Canonical gate order: owner →
    setup/drafting/disclosure (shared gate_cloud_llm) → caps."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        try:
            gate_cloud_llm(
                ws=ws,
                request=request,
                session=session,
                user=user,
                disclosure_action=_DISCLOSURE_ACTION,
            )
        except HTTPException as exc:
            # gate_cloud_llm raises 503 for the drafting kill-switch. On /api the
            # >=500 handler (main.py:361) sanitizes that to "internal_error",
            # discarding the message — so remap to a retriable 429 (the read-model's
            # can_send is the proactive surface; this is only a race backstop).
            if exc.status_code == 503:
                raise HTTPException(
                    status_code=429, detail=exc.detail, headers={"Retry-After": "30"}
                ) from exc
            raise
        # One-in-flight guard: the DB only blocks two RUNNING jobs (turn_jobs
        # partial index), not two QUEUED. Reject a second submit while a turn is
        # queued/running. Endpoint-level (not under the mutex): the residual race
        # is benign — owner-only + the composer is disabled while a turn runs +
        # the running partial-index is the hard backstop; two queued turns would
        # only process sequentially, never corrupt.
        active = session.scalar(
            select(TurnJob.id)
            .where(
                TurnJob.workspace_id == workspace_id,
                TurnJob.status.in_(("queued", "running")),
            )
            .limit(1)
        )
        if active is not None:
            raise HTTPException(status_code=409, detail="A turn is already in progress.")
        try:
            job = submit_user_turn(
                session=session,
                workspace_id=workspace_id,
                content_text=body.content,
                reference_ids=[uuid.UUID(r) for r in body.reference_ids],
                submitted_by_user_id=user.id,
                settings=settings,
                workspaces_dir=Path(settings.workspaces_dir),
            )
        except (TokenCapExceeded, GlobalTokenCapExceeded) as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except CommitMutexTimeout as exc:
            # submit_user_turn acquires the per-workspace commit mutex; busy → the
            # codebase's standard retriable 429 (mirrors api/workspaces.py:733).
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from exc
        except ValueError as exc:  # cross-workspace reference id
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.commit()
        session.refresh(job)
        user_turn = session.get(ChatTurn, job.user_turn_id)
        if user_turn is None:
            raise HTTPException(status_code=500, detail="user_turn missing after submit")
        return SubmitTurnResponse(job_id=str(job.id), user_turn=TurnOut(**_turn_to_dict(user_turn)))


class TurnsResponse(BaseModel):
    turns: list[TurnOut]
    can_send: bool
    needs_disclosure: bool
    phase: str
    running_job_id: str | None


@router.get("/workspaces/{workspace_id}/turns", response_model=TurnsResponse)
def list_turns(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> TurnsResponse:
    """Chat read-model. The frontend gates the composer proactively from these
    flags (mirrors the submit gate order), so a 4xx is only ever a race backstop."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        rows = list(
            session.scalars(
                select(ChatTurn)
                .where(ChatTurn.workspace_id == workspace_id)
                .order_by(ChatTurn.position)
            )
        )
        prior_ack = session.scalar(
            select(WorkspaceEvent.id).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.kind == "disclosure_acknowledged",
                WorkspaceEvent.actor_id == user.id,
            ).limit(1)
        )
        running = session.scalar(
            select(TurnJob.id)
            .where(TurnJob.workspace_id == workspace_id, TurnJob.status.in_(("queued", "running")))
            .order_by(TurnJob.queued_at.desc())
            .limit(1)
        )
        is_owner = ws.owner_id == user.id
        drafting_on = request.app.state.settings.drafting_enabled
        can_send = bool(is_owner and ws.current_phase == "active" and drafting_on)
        return TurnsResponse(
            turns=[TurnOut(**_turn_to_dict(t)) for t in rows],
            can_send=can_send,
            needs_disclosure=prior_ack is None,
            phase=ws.current_phase,
            running_job_id=str(running) if running else None,
        )
