from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    User,
    UserCallDismissal,
)
from call_manager.web.call_detail_view import _criterion_detail, _parse_optional_profile_id
from call_manager.web.dashboard import build_dashboard_view, resolve_profile_selection
from call_manager.web.filters import (
    criterion_label,
    deadline_local_date,
    model_label,
    note_category_heading,
)

router = APIRouter()


class CallOut(BaseModel):
    id: int
    title: str
    # Typed as datetime so the OpenAPI schema emits `format: date-time`.
    # Pydantic v2 serializes tz-aware datetimes to RFC 3339; a UTC value renders
    # with a `Z` suffix (e.g. ...T12:30:45Z) — NOT the `+00:00` the prior _iso()
    # str passthrough produced. Same instant, both valid RFC 3339; the
    # `format: date-time` contract lets the Plan 3 TS client parse either form.
    deadline: datetime | None
    # Pre-localized calendar date string (YYYY-MM-DD, Europe/Stockholm) for the
    # deadline — single source of truth so the SPA and the digest email agree
    # even across DST boundaries. None when deadline is not set.
    deadline_local_date: str | None
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


class CallDetailCallOut(BaseModel):
    id: int
    title: str
    source_name: str | None
    programme: str | None
    external_id: str | None
    published_at: datetime | None
    deadline: datetime | None
    # Pre-localized calendar date string (YYYY-MM-DD, Europe/Stockholm). None when
    # deadline is not set. Computed by the same shared helper as the digest email.
    deadline_local_date: str | None
    budget_min: int | None
    budget_max: int | None
    currency: str | None
    url: str
    keywords: list[str]
    summary: str | None


class CallDetailNoteOut(BaseModel):
    category: str | None
    heading: str
    text: str


class CallDetailRankingOut(BaseModel):
    aggregate_score: int
    aggregate_reason_label: str | None
    model_label: str
    rationale: str
    embed_similarity: float | None
    escalated: bool
    escalated_from_score: int | None
    analysis_model_label: str
    summary: str | None
    applicant_notes: list[CallDetailNoteOut]


class CallDetailCriterionOut(BaseModel):
    criterion: str
    label: str
    rating: str
    is_topic_fit: bool
    detail: str | None
    rationale: str | None
    is_reason_code: bool
    quoted_evidence: str | None


class CallDetailOut(BaseModel):
    call: CallDetailCallOut
    ranking: CallDetailRankingOut | None
    criteria: list[CallDetailCriterionOut]
    best_profile_id: int | None


class CallHideOut(BaseModel):
    hidden: bool


class HiddenCallOut(BaseModel):
    id: int
    title: str
    deadline: datetime | None
    deadline_local_date: str | None
    source_name: str | None


class HiddenListOut(BaseModel):
    items: list[HiddenCallOut]


@router.post("/calls/{call_id}/hide", response_model=CallHideOut)
def hide_call(
    request: Request,
    call_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        # ON CONFLICT DO NOTHING — idempotent if the user hides twice.
        session.execute(
            pg_insert(UserCallDismissal)
            .values(user_id=user.id, call_id=call_id)
            .on_conflict_do_nothing(index_elements=["user_id", "call_id"])
        )
        session.commit()
    return CallHideOut(hidden=True)


@router.delete("/calls/{call_id}/hide", response_model=CallHideOut)
def unhide_call(
    request: Request,
    call_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Remove a dismissal so the call reappears in the feed. Idempotent — 200
    even when the call was not hidden (no dismissal row to delete)."""
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        session.execute(
            delete(UserCallDismissal).where(
                UserCallDismissal.user_id == user.id,
                UserCallDismissal.call_id == call_id,
            )
        )
        session.commit()
    return CallHideOut(hidden=False)


@router.get("/calls/hidden", response_model=HiddenListOut)
def list_hidden_calls(
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Return the calls the current user has hidden, ordered newest-dismissed first."""
    sf = request.app.state.session_factory
    with sf() as session:
        rows = session.execute(
            select(Call, UserCallDismissal.dismissed_at)
            .join(
                UserCallDismissal,
                (UserCallDismissal.call_id == Call.id)
                & (UserCallDismissal.user_id == user.id),
            )
            .options(joinedload(Call.source))
            .order_by(UserCallDismissal.dismissed_at.desc())
        ).all()
        items = [
            HiddenCallOut(
                id=call.id,
                title=call.title,
                deadline=call.deadline,
                deadline_local_date=deadline_local_date(call.deadline),
                source_name=(call.source.name if call.source else None),
            )
            for call, _dismissed_at in rows
        ]
    return HiddenListOut(items=items)


@router.get("/calls/{call_id}", response_model=CallDetailOut)
def get_call_detail(
    request: Request,
    call_id: int,
    profile: str | None = Query(default=None),
    user: User = Depends(get_current_user),  # noqa: B008
):
    profile_id = _parse_optional_profile_id(profile)  # empty -> None, bad -> 400
    sf = request.app.state.session_factory
    with sf() as session:
        call = session.execute(
            select(Call).options(joinedload(Call.source)).where(Call.id == call_id)
        ).scalar_one_or_none()
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")

        ranking = None
        criteria: list[CallRankingCriterion] = []
        profile_obj: Profile | None = None
        # Parity: resolve ?profile by is_active ALONE — NOT ownership (web/routes/calls.py:69-71).
        if profile_id is not None:
            p = session.get(Profile, profile_id)
            if p is not None and p.is_active:
                profile_obj = p
                ranking = session.execute(
                    select(CallRanking).where(
                        CallRanking.call_id == call_id,
                        CallRanking.profile_id == profile_id,
                    )
                ).scalar_one_or_none()
                if ranking is not None:
                    criteria = list(
                        session.execute(
                            select(CallRankingCriterion).where(
                                CallRankingCriterion.call_id == call_id,
                                CallRankingCriterion.profile_id == profile_id,
                            )
                        ).scalars()
                    )

        # best-fit profile: highest-scoring profile OWNED by the current user.
        best_profile_id = session.scalar(
            select(CallRanking.profile_id)
            .join(Profile, Profile.id == CallRanking.profile_id)
            .where(CallRanking.call_id == call_id, Profile.created_by == user.id)
            .order_by(
                CallRanking.aggregate_score.desc().nullslast(),
                CallRanking.embed_similarity.desc().nullslast(),
            )
            .limit(1)
        )

        call_out = CallDetailCallOut(
            id=call.id,
            title=call.title,
            source_name=(call.source.name if call.source else None),
            programme=call.programme,
            external_id=call.external_id,
            published_at=call.published_at,
            deadline=call.deadline,
            deadline_local_date=deadline_local_date(call.deadline),
            budget_min=call.budget_min,
            budget_max=call.budget_max,
            currency=call.currency,
            url=call.url,
            keywords=list(call.keywords or []),
            summary=call.summary,
        )

        ranking_out = None
        if ranking is not None:
            notes = [
                CallDetailNoteOut(
                    category=n.get("category"),
                    heading=note_category_heading(n.get("category")),
                    text=n.get("text", ""),
                )
                for n in (ranking.applicant_notes or [])
            ]
            ranking_out = CallDetailRankingOut(
                aggregate_score=ranking.aggregate_score,
                aggregate_reason_label=(
                    criterion_label(ranking.aggregate_reason)
                    if ranking.aggregate_reason
                    else None
                ),
                model_label=model_label(ranking.model),
                rationale=ranking.rationale,
                embed_similarity=ranking.embed_similarity,
                escalated=ranking.escalated,
                escalated_from_score=ranking.escalated_from_score,
                analysis_model_label=model_label(ranking.analysis_model or ranking.model),
                summary=ranking.summary,
                applicant_notes=notes,
            )

        criteria_out = [
            CallDetailCriterionOut(
                criterion=c.criterion,
                label=criterion_label(c.criterion),
                rating=c.rating,
                is_topic_fit=(c.criterion == "topic_fit"),
                detail=(
                    _criterion_detail(c.criterion, call=call, profile=profile_obj, ranking=ranking)
                    if (profile_obj is not None and ranking is not None)
                    else None
                ),
                rationale=c.rationale,
                is_reason_code=(c.criterion in ("budget", "timeline", "trl", "embedding")),
                quoted_evidence=c.quoted_evidence,
            )
            for c in criteria
        ]

    return CallDetailOut(
        call=call_out, ranking=ranking_out, criteria=criteria_out, best_profile_id=best_profile_id
    )


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
                deadline_local_date=deadline_local_date(call.deadline),
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
