"""JSON /api surface for workspace settings + collaborators (Cockpit Slice D).

OWNER-GATED. Reuses the Jinja validation constants/parser (_WORKSPACE_STATUSES,
_CITATION_STYLES, _parse_deadline_form, _MAX_WORKSPACE_TITLE_LEN) and the
require_owner guard, inlining the same orchestration the Jinja post_settings /
add_viewer / remove_viewer handlers run. No new shared helper; the Jinja routes
are deleted at Slice G. No DB schema change.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User, WorkspaceEvent, WorkspaceViewer
from call_manager.proposal_assist.web._perms import require_owner
from call_manager.proposal_assist.web.workspaces import (
    _CITATION_STYLES,
    _MAX_WORKSPACE_TITLE_LEN,
    _WORKSPACE_STATUSES,
    _parse_deadline_form,
)
from call_manager.web.api.workspaces import MemberOut

router = APIRouter()


class SettingsOut(BaseModel):
    title: str
    status: str
    deadline: str | None
    citation_style: str


class SettingsUpdateIn(BaseModel):
    title: str
    status: str
    deadline: str | None = None
    citation_style: str


def _settings_out(ws) -> SettingsOut:
    return SettingsOut(
        title=ws.title,
        status=ws.status,
        deadline=ws.deadline.date().isoformat() if ws.deadline else None,
        citation_style=ws.citation_style,
    )


@router.get("/workspaces/{workspace_id}/settings", response_model=SettingsOut)
def get_settings(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SettingsOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        return _settings_out(ws)


@router.patch("/workspaces/{workspace_id}/settings", response_model=SettingsOut)
def update_settings(
    workspace_id: uuid.UUID,
    body: SettingsUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SettingsOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        if body.status not in _WORKSPACE_STATUSES:
            raise HTTPException(status_code=400, detail="invalid status")
        if body.citation_style not in _CITATION_STYLES:
            raise HTTPException(status_code=400, detail="invalid citation_style")
        title = body.title.strip()
        if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
            raise HTTPException(status_code=400, detail="invalid title")
        ws.title = title
        ws.status = body.status
        ws.deadline = _parse_deadline_form(body.deadline)  # HTTPException(400) on malformed
        ws.citation_style = body.citation_style
        session.commit()
        return _settings_out(ws)


class AddMemberIn(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/members", response_model=MemberOut, status_code=201)
def add_member(
    workspace_id: uuid.UUID,
    body: AddMemberIn,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),  # noqa: B008
) -> MemberOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        target = session.scalar(select(User).where(User.email == body.email))
        # Generic failure (no "no such user" oracle), mirroring the Jinja add_viewer.
        if target is None:
            raise HTTPException(status_code=400, detail="could not add collaborator")
        if target.id == ws.owner_id:
            response.status_code = 200  # owner already has full access — no-op
            return MemberOut(user_id=target.id, email=target.email, role="owner")
        existing = (
            session.query(WorkspaceViewer).filter_by(workspace_id=ws.id, user_id=target.id).first()
        )
        if existing is not None:
            response.status_code = 200  # idempotent
            return MemberOut(user_id=target.id, email=target.email, role="member")
        session.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="member_added",
                actor_id=user.id,
                payload={"target_user_id": target.id, "target_email": target.email},
            )
        )
        session.commit()
        return MemberOut(user_id=target.id, email=target.email, role="member")


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(
    workspace_id: uuid.UUID,
    user_id: int,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> Response:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        target = session.get(User, user_id)  # resolve email for the audit payload
        deleted = (
            session.query(WorkspaceViewer).filter_by(workspace_id=ws.id, user_id=user_id).delete()
        )
        if deleted:
            session.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="member_removed",
                    actor_id=user.id,
                    payload={
                        "target_user_id": user_id,
                        "target_email": target.email if target else None,
                    },
                )
            )
        session.commit()
    return Response(status_code=204)
