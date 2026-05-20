"""Owner rubric-review endpoint (Phase 0 confirmation gate).

Workspace owners review the auto-extracted rubric (or edit it) and click
Confirm. POST handler (Task 18) advances the workspace from current_phase
'setup' to 'spec' and marks the workspace_setup_jobs row 'done'.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Workspace
from call_monitor.db.session import get_db_dependency

router = APIRouter(prefix="/workspaces", tags=["proposal-assist"])


def _require_owner(
    *,
    workspace_id: uuid.UUID,
    user,
    session: Session,
) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="only the workspace owner may view or edit the rubric",
        )
    return ws


def _session_dep(request: Request):
    """Resolve the per-request session from the app's factory.
    `get_db_dependency` is a factory that returns the actual dependency
    callable; we wire it via app.state to avoid a global at import time."""
    factory = request.app.state.session_factory
    yield from get_db_dependency(factory)()


@router.get(
    "/{workspace_id}/rubric-review",
    response_class=HTMLResponse,
)
def get_rubric_review(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
):
    ws = _require_owner(
        workspace_id=workspace_id,
        user=user,
        session=session,
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "proposal_assist/rubric_review.html",
        {
            "workspace": ws,
            "criteria": ws.rubric_criteria,
            "funder_notes": ws.rubric_funder_notes or "",
        },
    )
