from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
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

router = APIRouter()


# The dashboard index (`GET /`) was retired in Plan 3 Task 9: the React SPA now
# owns `/` (served by Vite in dev / the prod catch-all), and the read-model
# (`build_dashboard_view`) is reached only via the JSON `GET /api/calls`. This
# router now serves the still-server-rendered companion pages: the call-detail
# page and the hide action. The sticky-default WRITE that used to live in
# `calls_index` moved to `POST /api/me/default-profile`. (The HTMX
# ranking-status strip was retired with the Jinja dashboard — the SPA renders
# ranking progress client-side from the `ranking` block of `GET /api/calls`.)
@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_detail(
    request: Request,
    call_id: int,
    profile: str | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
):
    profile_id = _parse_optional_profile_id(profile)
    profile = profile_id  # type: ignore[assignment]
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        call = session.execute(
            select(Call).options(joinedload(Call.source)).where(Call.id == call_id)
        ).scalar_one_or_none()
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")
        ranking = None
        criteria = []
        profile_obj: Profile | None = None
        if profile is not None:
            p = session.get(Profile, profile)
            if p is not None and p.is_active:
                profile_obj = p
                ranking = session.execute(
                    select(CallRanking).where(
                        CallRanking.call_id == call_id,
                        CallRanking.profile_id == profile,
                    )
                ).scalar_one_or_none()
                if ranking is not None:
                    criteria = (
                        session.execute(
                            select(CallRankingCriterion).where(
                                CallRankingCriterion.call_id == call_id,
                                CallRankingCriterion.profile_id == profile,
                            )
                        )
                        .scalars()
                        .all()
                    )
        # B6: best-fit profile lookup — highest-scoring profile among those
        # owned by the current user for this call. nulls_last() ensures a
        # profile with NULL score never displaces one with a real score.
        best_profile_id = session.scalar(
            select(CallRanking.profile_id)
            .join(Profile, Profile.id == CallRanking.profile_id)
            .where(
                CallRanking.call_id == call_id,
                Profile.created_by == user.id,
            )
            .order_by(
                CallRanking.aggregate_score.desc().nullslast(),
                CallRanking.embed_similarity.desc().nullslast(),
            )
            .limit(1)
        )

        # Detach the Call AND its joined `source` together (joinedload at the
        # top already populated `source`) so the template can read `call.source`
        # after the session closes.
        session.expunge_all()

    # Pre-compute the "why pass / why fail" detail string for each criterion
    # row so the template can stay declarative. None for criteria we don't
    # enrich (topic_fit / eligibility / exclusions already carry rationale).
    details: dict[str, str | None] = {}
    if profile_obj is not None and ranking is not None:
        for c in criteria:
            details[c.criterion] = _criterion_detail(
                c.criterion, call=call, profile=profile_obj, ranking=ranking
            )

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="call_detail.html",
        context={
            "call": call,
            "ranking": ranking,
            "criteria": criteria,
            "criterion_details": details,
            "best_profile_id": best_profile_id,
            "user": user,
        },
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
