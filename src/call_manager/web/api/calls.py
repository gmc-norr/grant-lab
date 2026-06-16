from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.web.dashboard import build_dashboard_view, resolve_profile_selection

router = APIRouter()


class CallOut(BaseModel):
    id: int
    title: str
    deadline: str | None
    published_at: str | None
    source_name: str | None
    source_slug: str | None
    aggregate_score: float | None


class DashboardOut(BaseModel):
    items: list[CallOut]
    summary: dict[str, int]
    pagination: dict[str, object]
    ranking: dict[str, int]
    effective: dict[str, object]
    facets: dict[str, list[dict]]


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


@router.get("/calls", response_model=DashboardOut)
def get_calls(
    request: Request,
    source: str | None = Query(default=None),
    sort: Literal["newest", "deadline"] = Query(default="newest"),
    profile: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    page: int = Query(default=1, ge=1),
    show_closed: bool = Query(default=False),
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        # READ-ONLY: persist=False — a deep link never changes the stored default.
        profile_id = resolve_profile_selection(
            session, user_id=user.id, raw_profile=profile, persist=False
        )
        view = build_dashboard_view(
            session,
            user_id=user.id,
            source=source,
            sort=sort,
            profile_id=profile_id,
            limit=limit,
            page=page,
            show_closed=show_closed,
        )
        items = [
            CallOut(
                id=call.id,
                title=call.title,
                deadline=_iso(call.deadline),
                published_at=_iso(call.published_at),
                source_name=(call.source.name if call.source else None),
                source_slug=(call.source.slug if call.source else None),
                aggregate_score=(ranking.aggregate_score if ranking else None),
            )
            for call, ranking in view.calls
        ]
        facets = {
            "sources": [{"slug": s.slug, "name": s.name} for s in view.sources],
            "profiles": [{"id": p.id, "name": p.name} for p in view.profiles],
        }
    return DashboardOut(
        items=items,
        summary=view.summary,
        pagination=view.pagination,
        ranking=view.ranking,
        effective=view.effective,
        facets=facets,
    )
