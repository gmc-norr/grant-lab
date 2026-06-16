from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
from call_manager.web.dashboard import _ranking_counts


def _parse_optional_profile_id(raw: str | None) -> int | None:
    """HTML <select> filters submit `?profile=` (empty string) when "all"
    is chosen. FastAPI's `int | None` declaration refuses to parse an
    empty string as int — so the param is accepted as `str | None` and
    we coerce here, treating empty/whitespace identically to omission.
    A non-empty non-integer still 400s, since that's an actual bad URL."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid profile id: {raw!r}") from e


router = APIRouter()

# The dashboard index (`GET /`) was retired in Plan 3 Task 9: the React SPA now
# owns `/` (served by Vite in dev / the prod catch-all), and the read-model
# (`build_dashboard_view`) is reached only via the JSON `GET /api/calls`. This
# router now serves the still-server-rendered companion pages: the call-detail
# page, the HTMX ranking-status strip, and the hide action. The sticky-default
# WRITE that used to live in `calls_index` moved to `POST /api/me/default-profile`.


# NOTE: must be declared before /calls/{call_id} — FastAPI matches routes in
# declaration order, so "ranking-status" would otherwise be captured as call_id.
@router.get("/calls/ranking-status", response_class=HTMLResponse)
def ranking_status(
    request: Request,
    profile: int = Query(...),
    poll: int = Query(default=0),
    user: User = Depends(get_current_user),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        pending, failed = _ranking_counts(session, profile)
    if pending == 0 and poll == 1:
        # Drained while polling → reload the feed so the new scores show.
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/ranking_status_strip.html",
        context={"pending": pending, "failed": failed, "profile_id": profile},
    )


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

        # See note in calls_index: detach Call AND its joined `source` together.
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


def _criterion_detail(
    name: str, *, call: Call, profile: Profile, ranking: CallRanking
) -> str | None:
    """One-line "why" string for a deterministic gate criterion. Returns
    None for LLM criteria (topic_fit / eligibility / exclusions) — their
    rationale field already carries a free-text explanation.

    Generated at render time so profile/call edits made AFTER the last
    ranking are reflected immediately — the ratings are point-in-time
    snapshots, the displayed reasoning is "as the rubric looks today"."""
    p = profile.structured or {}
    c_struct = call.structured or {}
    if name == "timeline":
        if call.deadline is None:
            return "No deadline declared by source — treated as PASS."
        delta = call.deadline - datetime.now(UTC)
        days = delta.days
        if days < 0:
            return f"Deadline passed {-days} day(s) ago ({call.deadline.date()})."
        return f"Deadline {call.deadline.date()} — {days} day(s) away (minimum 14 day window)."
    if name == "budget":
        c_min, c_max = call.budget_min, call.budget_max
        p_min, p_max = p.get("budget_min"), p.get("budget_max")
        c_cur = (call.currency or "?").upper()
        p_cur = (profile.budget_currency or "?").upper()
        c_str = _money_range(c_min, c_max, c_cur)
        p_str = _money_range(p_min, p_max, p_cur)
        if c_min is None or c_max is None or p_min is None or p_max is None:
            return f"Call: {c_str} · Profile: {p_str} — incomplete data, gate UNKNOWN."
        if c_cur != p_cur and "?" not in (c_cur, p_cur):
            return f"Call: {c_str} · Profile: {p_str} — currency mismatch."
        return f"Call: {c_str} · Profile: {p_str}."
    if name == "trl":
        c_min, c_max = c_struct.get("trl_min"), c_struct.get("trl_max")
        p_min, p_max = p.get("trl_min"), p.get("trl_max")
        c_str = _trl_range(c_min, c_max)
        p_str = _trl_range(p_min, p_max)
        if None in (c_min, c_max, p_min, p_max):
            return f"Call: {c_str} · Profile: {p_str} — incomplete data, gate UNKNOWN."
        return f"Call: {c_str} · Profile: {p_str}."
    if name == "embedding":
        sim = ranking.embed_similarity
        if sim is None:
            return "No cosine — one or both vectors missing, gate UNKNOWN."
        return f"Cosine similarity {sim:.3f} (pass threshold 0.30)."
    return None


def _money_range(lo: int | None, hi: int | None, currency: str) -> str:
    if lo is None and hi is None:
        return "(unspecified)"
    lo_s = f"{lo:,}" if lo is not None else "—"
    hi_s = f"{hi:,}" if hi is not None else "—"
    return f"{lo_s}–{hi_s} {currency}"


def _trl_range(lo, hi) -> str:
    if lo is None and hi is None:
        return "(unspecified)"
    return f"{lo if lo is not None else '—'}–{hi if hi is not None else '—'}"


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
