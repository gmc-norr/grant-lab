from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from call_monitor.auth.dependencies import get_current_user
from call_monitor.db.models import Call, Source, User

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
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        stmt = (
            select(Call)
            .options(joinedload(Call.source))
            .order_by(*_order_for(sort))
            .limit(limit)
        )
        if source:
            stmt = stmt.where(
                Call.source_id == select(Source.id).where(Source.slug == source).scalar_subquery()
            )
        calls = session.execute(stmt).scalars().unique().all()
        sources = session.execute(select(Source).order_by(Source.name)).scalars().all()
        # expunge_all() detaches everything in the identity map — including the
        # `source` rows reached via joinedload(Call.source). Per-object expunge
        # leaves them attached and the template's `c.source.name` lookup would
        # raise DetachedInstanceError once the session closes below.
        session.expunge_all()

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="calls_index.html",
        context={
            "calls": calls,
            "sources": sources,
            "selected_source": source,
            "selected_sort": sort,
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
