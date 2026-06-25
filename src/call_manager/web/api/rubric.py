"""JSON /api surface for the funder rubric + setup gate (Cockpit Slice E).

GET rubric / activate / PUT rubric are OWNER-gated; GET setup-status is member-
gated (last_error owner-only). Replaces the Jinja rubric-review + setup-status
routes (deleted in this slice). No DB schema change.

# NOTE: Task 2 appends the write endpoints (activate, PUT rubric) + _apply_rubric
# and EXTENDS the imports below (HTTPException, WorkspaceEvent, datetime/UTC).
# They are intentionally absent here so Task 1's `ruff check` stays F401-clean.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User, WorkspaceSetupJob
from call_manager.proposal_assist.web._perms import require_member, require_owner

router = APIRouter()


class CriterionOut(BaseModel):
    # extra="allow" round-trips passthrough fields (e.g. subcriteria) the editor
    # must not drop. Serialization includes them; OpenAPI emits additionalProperties.
    model_config = ConfigDict(extra="allow")
    name: str
    description: str
    weight: float
    scale_max: int


class RubricOut(BaseModel):
    criteria: list[CriterionOut]
    funder_notes: str | None
    updated_at: object  # a datetime; FastAPI serializes it to ISO at runtime


class SetupStatusOut(BaseModel):
    status: str
    last_error: str | None


def _rubric_out(ws) -> RubricOut:
    return RubricOut(
        criteria=[CriterionOut(**c) for c in (ws.rubric_criteria or [])],
        funder_notes=ws.rubric_funder_notes,
        updated_at=ws.rubric_updated_at,
    )


@router.get("/workspaces/{workspace_id}/rubric", response_model=RubricOut)
def get_rubric(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        return _rubric_out(ws)


@router.get("/workspaces/{workspace_id}/setup-status", response_model=SetupStatusOut)
def get_setup_status(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SetupStatusOut:
    with request.app.state.session_factory() as session:
        ws = require_member(workspace_id=workspace_id, user=user, session=session)
        job = session.scalar(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id)
        )
        is_owner = ws.owner_id == user.id
        return SetupStatusOut(
            status=job.status if job else "queued",
            last_error=(job.last_error if job else None) if is_owner else None,
        )
