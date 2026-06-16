from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.web.dashboard import build_dashboard_view, resolve_profile_selection

router = APIRouter()


class CallOut(BaseModel):
    id: int
    title: str
    # Typed as datetime so the OpenAPI schema emits `format: date-time`.
    # Pydantic v2 serializes tz-aware datetimes to ISO-8601 (with the +00:00
    # offset), so the wire value is unchanged from the prior str passthrough.
    deadline: datetime | None
    published_at: datetime | None
    source_name: str | None
    source_slug: str | None
    # None = no ranking row for this call/profile (the score itself is NOT NULL).
    aggregate_score: int | None


class SummaryOut(BaseModel):
    open_count: int
    high_score: int
    soon_deadline: int


class PaginationOut(BaseModel):
    page: int
    limit: int
    has_prev: bool
    has_next: bool
    total: int


class EffectiveOut(BaseModel):
    source: str | None
    sort: Literal["newest", "deadline"]
    profile_id: int | None = Field(
        description=(
            "The RESOLVED profile id, not the requested one. An unknown or "
            "inactive requested id is degraded to null (no error), so a client "
            "cannot distinguish a silently-ignored selection from 'no selection'."
        )
    )
    show_closed: bool


class RankingOut(BaseModel):
    pending: int
    failed: int


class SourceFacet(BaseModel):
    slug: str
    name: str


class ProfileFacet(BaseModel):
    id: int
    name: str


class FacetsOut(BaseModel):
    sources: list[SourceFacet]
    profiles: list[ProfileFacet]


class DashboardOut(BaseModel):
    items: list[CallOut]
    summary: SummaryOut
    pagination: PaginationOut
    ranking: RankingOut
    effective: EffectiveOut
    facets: FacetsOut


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
                deadline=call.deadline,
                published_at=call.published_at,
                source_name=(call.source.name if call.source else None),
                source_slug=(call.source.slug if call.source else None),
                aggregate_score=(ranking.aggregate_score if ranking else None),
            )
            for call, ranking in view.calls
        ]
        facets = FacetsOut(
            sources=[SourceFacet(slug=s.slug, name=s.name) for s in view.sources],
            profiles=[ProfileFacet(id=p.id, name=p.name) for p in view.profiles],
        )
    return DashboardOut(
        items=items,
        summary=view.summary,
        pagination=view.pagination,
        ranking=view.ranking,
        effective=view.effective,
        facets=facets,
    )
