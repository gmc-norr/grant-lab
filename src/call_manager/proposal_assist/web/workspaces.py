"""B6 workspace web UX — list, create, main two-pane view, settings,
turn submission + SSE bridge, viewer mgmt, disclosure modal, export.

All routes mount at /workspaces. Mirrors the proposal_assist router
pattern from rubric_review.py (same _session_dep, same auth dep).

Rev2 / Codex M2: uses `request.app.state.templates` (set up by
main.py) — NOT a fresh Jinja2Templates instance. This is what keeps
the navigation in base.html working (base.html expects `user` in the
context, and main.py registers the `display_dt` / `criterion_label`
/ ... filters on that single instance).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Workspace, WorkspaceViewer
from call_manager.db.session import get_db_dependency

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _session_dep(request: Request):
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()


def _load_workspace_or_404(
    workspace_id: uuid.UUID,
    session: Session,
) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.soft_deleted_at is not None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return ws


def _require_owner(
    *,
    workspace_id: uuid.UUID,
    user,
    session: Session,
) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if ws.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the workspace owner may perform this action",
        )
    return ws


def _require_owner_or_viewer(
    *,
    workspace_id: uuid.UUID,
    user,
    session: Session,
) -> Workspace:
    ws = _load_workspace_or_404(workspace_id, session)
    if ws.owner_id == user.id:
        return ws
    is_viewer = (
        session.query(WorkspaceViewer)
        .filter_by(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        .first()
        is not None
    )
    if not is_viewer:
        raise HTTPException(
            status_code=403,
            detail="you do not have access to this workspace",
        )
    return ws


@router.get("/{workspace_id}", response_class=HTMLResponse)
def get_workspace_main(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
) -> HTMLResponse:
    """Two-pane main view. Task 5 fills in chat history + artifact
    pane; this stub renders the shell.

    Uses `request.app.state.templates` (the existing instance with
    registered filters + base.html nav). Context key is `user`
    (NOT `current_user`) to match base.html.
    """
    ws = _require_owner_or_viewer(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "proposal_assist/workspace_main.html",
        {"workspace": ws, "user": user},
    )
