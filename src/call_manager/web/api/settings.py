"""JSON /api surface for workspace settings + collaborators (Cockpit Slice D).

OWNER-GATED. Reuses the Jinja validation constants/parser (_WORKSPACE_STATUSES,
_CITATION_STYLES, _parse_deadline_form, _MAX_WORKSPACE_TITLE_LEN) and the
require_owner guard, inlining the same orchestration the Jinja post_settings /
add_viewer / remove_viewer handlers run. No new shared helper; the Jinja routes
are deleted at Slice G. No DB schema change.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.proposal_assist.web._perms import require_owner
from call_manager.proposal_assist.web.workspaces import (
    _CITATION_STYLES,
    _MAX_WORKSPACE_TITLE_LEN,
    _WORKSPACE_STATUSES,
    _parse_deadline_form,
)

# NOTE: Task 3 adds the member routes and ALSO extends this import block with
# `Response` (fastapi), `select` (sqlalchemy), `WorkspaceEvent`/`WorkspaceViewer`
# (db.models), and `MemberOut` (web.api.workspaces). They are intentionally NOT
# imported here — Task 2 runs `ruff check` before Task 3, and unused imports fail F401.

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
