"""Member-gated read-only Activity feed (SP2b-3e).

GET returns a curated, reverse-chronological log of recent WorkspaceEvents —
collapsed, actor-resolved, with cheap deep-links. No mutation, no LLM, no new
table. Mirrors the Problems panel's read-only/polled/member-gated shape.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User, WorkspaceEvent
from call_manager.proposal_assist.activity.feed import (
    actor_user_id,
    build_activity_feed,
)
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/api/workspaces", tags=["activity"])

_ACTIVITY_WINDOW = 100


class ActivityTargetOut(BaseModel):
    top_tab: Literal["draft", "spec", "scorecard", "review"] | None
    bottom_tab: Literal["inbox", "problems", "activity"] | None


class ActivityRowOut(BaseModel):
    id: str
    category: Literal[
        "created", "edit", "citation", "turn", "review",
        "uncited", "dismissal", "reference", "rubric",
    ]
    actor: str | None
    summary: str
    count: int
    created_at: str
    target: ActivityTargetOut | None


class ActivityFeedOut(BaseModel):
    items: list[ActivityRowOut]


@router.get("/{workspace_id}/activity", response_model=ActivityFeedOut)
def get_activity(
    workspace_id: uuid.UUID,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    events = list(
        session.scalars(
            select(WorkspaceEvent)
            .where(WorkspaceEvent.workspace_id == workspace_id)
            .order_by(WorkspaceEvent.created_at.desc(), WorkspaceEvent.id.desc())
            .limit(_ACTIVITY_WINDOW)
        )
    )
    actor_ids = {uid for uid in (actor_user_id(e) for e in events) if uid is not None}
    actor_names: dict[int, str] = {}
    if actor_ids:
        actor_names = {
            uid: email
            for uid, email in session.execute(
                select(User.id, User.email).where(User.id.in_(actor_ids))
            )
        }
    rows = build_activity_feed(events, actor_names)
    return {"items": [r.as_dict() for r in rows]}
