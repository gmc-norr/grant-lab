"""Shared cloud-LLM trigger gate for the manual background-job endpoints
(coherence, evaluator, review). Extracted from the previously-duplicated
per-endpoint copies; the ONLY per-caller difference is the disclosure_action
phrase woven into the 403 message. Same order + status codes as the interactive
turns route (web/workspaces.py): setup -> 409, drafting disabled -> 503, missing
per-actor disclosure ack -> 403."""

from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import Workspace, WorkspaceEvent


def gate_cloud_llm(
    *, ws: Workspace, request: Request, session: Session, user, disclosure_action: str
) -> None:
    if ws.current_phase == "setup":
        raise HTTPException(
            status_code=409,
            detail="Workspace is still setting up — review the rubric to start drafting.",
        )
    if not request.app.state.settings.drafting_enabled:
        raise HTTPException(
            status_code=503,
            detail="Drafting is currently disabled by an administrator. Please try again later.",
        )
    prior_ack = session.scalar(
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == ws.id,
            WorkspaceEvent.kind == "disclosure_acknowledged",
            WorkspaceEvent.actor_id == user.id,
        )
        .limit(1)
    )
    if prior_ack is None:
        raise HTTPException(
            status_code=403,
            detail=f"Please acknowledge the data-disclosure notice before {disclosure_action}.",
        )
