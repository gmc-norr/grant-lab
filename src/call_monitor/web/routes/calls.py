from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Call, CallRanking, Profile, Source, User, UserCallDismissal

router = APIRouter()

# Per design spec: "Sort: score, deadline, newest." Score lands in Plan 3
# (per-profile). For Plan 2 we offer the two ordering primitives that don't
# depend on rankings.
_Sort = Literal["newest", "deadline"]


def _order_for(sort: _Sort):
    if sort == "deadline":
        # Soonest deadline first; never-expires (NULL) sink to the bottom.
        return (Call.deadline.asc().nullslast(), Call.id.desc())
    # "newest" (default): most recently published first; older or unknown
    # publication date sink to the bottom.
    return (Call.published_at.desc().nullslast(), Call.id.desc())


@router.get("/", response_class=HTMLResponse)
def calls_index(
    request: Request,
    source: str | None = Query(default=None),
    sort: _Sort = Query(default="newest"),  # noqa: B008
    profile: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        # Validate the requested profile is real AND active before
        # showing the ranked view. Spec (design spec, "Profile deletion
        # is soft") requires inactive profiles to be absent from feeds.
        # Unknown or inactive ?profile=<id> degrades to the unranked
        # list so a stale bookmark doesn't error or leak audit data.
        if profile is not None:
            exists_and_active = session.execute(
                select(Profile.id).where(
                    Profile.id == profile, Profile.is_active.is_(True)
                )
            ).scalar_one_or_none()
            if exists_and_active is None:
                profile = None

        # When a profile is selected, LEFT JOIN call_rankings for THAT
        # profile and sort by score desc (nulls last). Without a profile,
        # fall back to the unranked sort (newest/deadline).
        if profile is not None:
            stmt = (
                select(Call, CallRanking)
                .options(joinedload(Call.source))
                .outerjoin(
                    CallRanking,
                    (CallRanking.call_id == Call.id)
                    & (CallRanking.profile_id == profile),
                )
                .order_by(
                    CallRanking.score.desc().nullslast(),
                    Call.published_at.desc().nullslast(),
                    Call.id.desc(),
                )
                .limit(limit)
            )
        else:
            stmt = (
                select(Call, None)
                .options(joinedload(Call.source))
                .order_by(*_order_for(sort))
                .limit(limit)
            )
        if source:
            stmt = stmt.where(
                Call.source_id == select(Source.id).where(Source.slug == source).scalar_subquery()
            )
        dismissed_subq = (
            select(UserCallDismissal.call_id)
            .where(UserCallDismissal.user_id == user.id)
            .scalar_subquery()
        )
        stmt = stmt.where(Call.id.notin_(dismissed_subq))
        rows = session.execute(stmt).unique().all()
        # Normalize to (Call, CallRanking|None) shape regardless of branch
        calls_with_rankings: list[tuple[Call, CallRanking | None]] = [
            (row[0], row[1] if profile is not None else None) for row in rows
        ]
        sources = session.execute(select(Source).order_by(Source.name)).scalars().all()
        profiles = session.execute(
            select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name)
        ).scalars().all()
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="calls_index.html",
        context={
            "calls_with_rankings": calls_with_rankings,
            "sources": sources,
            "profiles": profiles,
            "selected_source": source,
            "selected_sort": sort,
            "selected_profile_id": profile,
            "user": user,
        },
    )


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(
    request: Request,
    call_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        call = session.execute(
            select(Call).options(joinedload(Call.source)).where(Call.id == call_id)
        ).scalar_one_or_none()
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")
        # See note in calls_index: detach Call AND its joined `source` together.
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="call_detail.html",
        context={"call": call, "user": user},
    )


@router.post("/calls/{call_id}/hide")
def hide_call(
    request: Request,
    call_id: int,
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        # ON CONFLICT DO NOTHING — idempotent if user clicks Hide twice
        session.execute(
            pg_insert(UserCallDismissal)
            .values(user_id=user.id, call_id=call_id)
            .on_conflict_do_nothing(index_elements=["user_id", "call_id"])
        )
        session.commit()
    return RedirectResponse(url="/", status_code=303)
