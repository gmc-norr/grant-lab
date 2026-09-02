"""Cockpit chat JSON/SSE turns surface (Slice B). Thin /api layer over the
existing propose-only turn engine: submit reuses submit_user_turn + the shared
cloud gate; the stream is the JSON analogue of the Jinja SSE bridge. No new
business logic, no schema change."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ChatTurn, TurnJob, User, WorkspaceEvent
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.turns.caps import GlobalTokenCapExceeded, TokenCapExceeded
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
from call_manager.proposal_assist.turns.selection import (
    SelectionConflictError,
    SelectionInput,
    build_selection_block,
)
from call_manager.proposal_assist.turns.submit import submit_user_turn
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm
from call_manager.proposal_assist.web._perms import is_member, require_member, require_owner

router = APIRouter()

# Anti-buffering headers shared by every SSE endpoint. Without these, Nginx
# and some reverse-proxies buffer the chunked body and the client sees no
# events until the buffer is flushed (or the stream closes).
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

_DISCLOSURE_ACTION = "sending a message to the drafting assistant"


class TurnOut(BaseModel):
    id: str
    role: str
    content: Any  # JSONB: str | {type:text,...} | list[block]
    created_at: str
    position: int


class SelectionContextIn(BaseModel):
    request_type: Literal["ask", "modify"]
    target_artifact: Literal["draft"] = "draft"
    section_index: int = Field(ge=0)
    base_commit_sha: str
    base_section_sha: str
    anchor: str = Field(min_length=1)
    from_offset: int = Field(ge=0)
    to_offset: int = Field(ge=0)


class SubmitTurnIn(BaseModel):
    content: str
    reference_ids: list[str] = []
    attachment_ids: list[str] = []
    selection_context: SelectionContextIn | None = None


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
    """Enqueue a user turn. Gate order: auth (owner — or any member for a
    selection *ask*, per spec §7) → setup/drafting/disclosure (shared
    gate_cloud_llm, per-actor) → caps. modify / free-chat stay owner-only."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        sc = body.selection_context
        if sc is not None and sc.request_type == "ask":
            # spec §7: Ask = any viewer (member). A member can ONLY submit a
            # selection ask — modify and free-chat (no selection) stay owner-only.
            ws = require_member(workspace_id=workspace_id, user=user, session=session)
        else:
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
        selection_block = None
        if sc is not None:
            repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
            sections = None
            collab_draft = sc.target_artifact == "draft" and collab_state.is_collab_enabled(
                session, workspace_id=workspace_id, artifact="draft"
            )
            if collab_draft:
                raw = canonical_artifact_markdown(
                    session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
                )
                sections = split_sections(raw)
            try:
                selection_block = build_selection_block(
                    repo_dir=repo_dir,
                    sel=SelectionInput(
                        request_type=sc.request_type,
                        section_index=sc.section_index,
                        base_commit_sha=sc.base_commit_sha,
                        base_section_sha=sc.base_section_sha,
                        anchor=sc.anchor,
                        from_offset=sc.from_offset,
                        to_offset=sc.to_offset,
                    ),
                    sections=sections,
                    allow_anchor_relocation=collab_draft,
                )
            except SelectionConflictError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
        try:
            job = submit_user_turn(
                session=session,
                workspace_id=workspace_id,
                content_text=body.content,
                attachment_ids=[uuid.UUID(a) for a in body.attachment_ids],
                reference_ids=[uuid.UUID(r) for r in body.reference_ids],
                submitted_by_user_id=user.id,
                settings=settings,
                workspaces_dir=Path(settings.workspaces_dir),
                selection_block=selection_block,
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


_TURNS_PAGE_DEFAULT = 500
_TURNS_PAGE_MAX = 1000


class TurnsResponse(BaseModel):
    turns: list[TurnOut]
    # Pagination (review 2026-09-02, carried: the list was unbounded). `turns` is
    # the newest `limit` turns in position order; `has_more` says older ones
    # exist and `next_before` is the cursor to pass as `before` to fetch them.
    has_more: bool = False
    next_before: int | None = None
    can_send: bool
    is_owner: bool
    disabled_reason: str | None
    needs_disclosure: bool
    phase: str
    running_job_id: str | None


@router.get("/workspaces/{workspace_id}/turns", response_model=TurnsResponse)
def list_turns(
    workspace_id: uuid.UUID,
    request: Request,
    limit: int = Query(_TURNS_PAGE_DEFAULT, ge=1, le=_TURNS_PAGE_MAX),
    before: int | None = Query(None, ge=0, description="return turns with position < before"),
    user: User = Depends(get_current_user),  # noqa: B008
) -> TurnsResponse:
    """Chat read-model. The frontend gates the composer proactively from these
    flags (mirrors the submit gate order), so a 4xx is only ever a race backstop."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        # Newest `limit` turns (one extra row tells us whether older ones exist),
        # returned in position order.
        stmt = select(ChatTurn).where(ChatTurn.workspace_id == workspace_id)
        if before is not None:
            stmt = stmt.where(ChatTurn.position < before)
        page = list(session.scalars(stmt.order_by(ChatTurn.position.desc()).limit(limit + 1)))
        has_more = len(page) > limit
        rows = list(reversed(page[:limit]))
        next_before = rows[0].position if has_more and rows else None
        prior_ack = session.scalar(
            select(WorkspaceEvent.id)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.kind == "disclosure_acknowledged",
                WorkspaceEvent.actor_id == user.id,
            )
            .limit(1)
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
        needs_disclosure = prior_ack is None
        # Canonical order mirrors gate_cloud_llm (setup -> drafting -> disclosure),
        # with not_owner prepended (submit is owner-only). disabled_reason is the
        # authoritative footer driver; can_send is retained for back-compat.
        if not is_owner:
            disabled_reason: str | None = "not_owner"
        elif ws.current_phase == "setup":
            disabled_reason = "setup"
        elif not drafting_on:
            disabled_reason = "drafting_disabled"
        elif needs_disclosure:
            disabled_reason = "needs_disclosure"
        else:
            disabled_reason = None
        return TurnsResponse(
            turns=[TurnOut(**_turn_to_dict(t)) for t in rows],
            has_more=has_more,
            next_before=next_before,
            can_send=can_send,
            is_owner=is_owner,
            disabled_reason=disabled_reason,
            needs_disclosure=needs_disclosure,
            phase=ws.current_phase,
            running_job_id=str(running) if running else None,
        )


class DisclosureResponse(BaseModel):
    acknowledged: bool


@router.post("/workspaces/{workspace_id}/disclosure", response_model=DisclosureResponse)
def ack_disclosure(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> DisclosureResponse:
    """Record the per-actor cloud-LLM disclosure acknowledgment (idempotent)."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
        prior = session.scalar(
            select(WorkspaceEvent.id)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.kind == "disclosure_acknowledged",
                WorkspaceEvent.actor_id == user.id,
            )
            .limit(1)
        )
        if prior is None:
            session.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="disclosure_acknowledged",
                    actor_id=user.id,
                )
            )
            session.commit()
    return DisclosureResponse(acknowledged=True)


_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def _identity_still_valid(session, *, workspace_id, user_id, session_epoch) -> bool:
    """The stream authorized once at connect; re-check on every poll (review
    2026-09-02, carried: it outlived logout, admin disable and membership
    removal). Same three facts get_current_user + require_member establish:
    active account, unrevoked epoch, still a member."""
    u = session.get(User, user_id)
    if u is None or u.status != "active" or u.session_epoch != session_epoch:
        return False
    return is_member(workspace_id=workspace_id, user=u, session=session)


async def _json_turn_stream(
    *,
    session_factory,
    workspace_id,
    turn_job_id,
    since_position=None,
    poll_seconds=0.2,
    request=None,
    user_id=None,
    session_epoch=None,
):
    """JSON analogue of the Jinja _stream_turn_generator: emit one `turn` event
    (with an `id:` cursor = ChatTurn.position) per new row, then a terminal
    `done` event. Honors since_position so useEventStream can resume on reconnect."""
    with session_factory() as s:
        job = s.get(TurnJob, turn_job_id)
        if job is None or job.workspace_id != workspace_id:
            yield 'event: done\ndata: {"status": "missing"}\n\n'
            return
        last_seen = (
            since_position
            if since_position is not None
            else (s.scalar(select(ChatTurn.position).where(ChatTurn.id == job.user_turn_id)) or 0)
        )
    while True:
        if request is not None and await request.is_disconnected():
            return
        with session_factory() as s:
            if user_id is not None and not _identity_still_valid(
                s, workspace_id=workspace_id, user_id=user_id, session_epoch=session_epoch
            ):
                yield 'event: done\ndata: {"status": "unauthorized"}\n\n'
                return
            tj = s.get(TurnJob, turn_job_id)
            terminal = tj is None or tj.status in _TERMINAL
            rows = list(
                s.scalars(
                    select(ChatTurn)
                    .where(ChatTurn.workspace_id == workspace_id, ChatTurn.position > last_seen)
                    .order_by(ChatTurn.position)
                )
            )
            for turn in rows:
                payload = json.dumps(_turn_to_dict(turn))
                yield f"id: {turn.position}\nevent: turn\ndata: {payload}\n\n"
                last_seen = turn.position
            if terminal:
                status = tj.status if tj is not None else "missing"
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return
            if not rows:
                # SSE comment: keeps proxies from idling the connection out and
                # gives the consumer a heartbeat between turns.
                yield ": keepalive\n\n"
        await asyncio.sleep(poll_seconds)


class CancelTurnResponse(BaseModel):
    cancelled: bool


@router.post("/workspaces/{workspace_id}/turns/{job_id}/cancel", response_model=CancelTurnResponse)
def cancel_turn(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> CancelTurnResponse:
    """Owner-only cancel of a QUEUED turn. Race-safe + cross-workspace-safe via
    record_terminal_outcome's conditional UPDATE (status='queued' AND
    workspace_id=…): a job already 'running', already terminal, or owned by
    another workspace → cancelled=False. Writes the cloud_turn_cancelled audit
    event + a visible turn_outcome chat row (emit_chat=True)."""
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        did = record_terminal_outcome(
            session,
            turn_job_id=job_id,
            workspace_id=ws.id,
            outcome="cancelled",
            expected_status="queued",
            raw_error=None,
            emit_chat=True,
        )
        session.commit()
    return CancelTurnResponse(cancelled=did)


@router.get("/workspaces/{workspace_id}/turns/{job_id}/stream", include_in_schema=False)
def stream_turn(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    request: Request,
    since: int | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
) -> StreamingResponse:
    """SSE stream of new chat turns for one job (owner OR viewer). EventSource
    can't set headers, so auth is the session cookie + require_member."""
    sf = request.app.state.session_factory
    with sf() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
    return StreamingResponse(
        _json_turn_stream(
            session_factory=sf,
            workspace_id=workspace_id,
            turn_job_id=job_id,
            since_position=since,
            request=request,
            user_id=user.id,
            session_epoch=user.session_epoch,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
