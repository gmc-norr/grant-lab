"""Member-gated read-only Problems dashboard (SP2b-3b).

GET /problems aggregates four cheap synchronous signals (broken citations,
criterion gaps, over-length sections, coherence drift) computed per call. No
caching, no LLM, no mutation, no new table.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.problems.aggregate import assemble_problems
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/api/workspaces", tags=["problems"])


@router.get("/{workspace_id}/problems")
def get_problems(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    return assemble_problems(
        session=session,
        workspace_id=workspace_id,
        repo_dir=repo_dir,
        ws_rubric_updated_at=ws.rubric_updated_at,
        budget=request.app.state.settings.problem_section_char_budget,
    )
