from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import joinedload

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import (
    Call,
    CallDecision,
    CallRanking,
    CallRankingCriterion,
    Profile,
    User,
)
from grant_lab.web.call_detail_view import _criterion_detail, _parse_optional_profile_id
from grant_lab.web.dashboard import (
    build_dashboard_view,
    count_decision_views,
    resolve_profile_selection,
)
from grant_lab.web.filters import (
    criterion_label,
    deadline_local_date,
    model_label,
    note_category_heading,
)
from grant_lab.web.profile_access import can_read_profile

router = APIRouter()


# Criteria whose rating is a machine-readable REASON CODE for the aggregate
# score rather than a graded judgement. Single-sourced here because both the
# list card and the detail page render the distinction.
_REASON_CODE_CRITERIA = ("budget", "timeline", "trl", "embedding")


def _criterion_common_fields(c: CallRankingCriterion) -> dict:
    """The criterion fields the list and the detail page MUST agree on.

    Copying the label lookup and the two derivations into the list response
    would let the card call "Budget" something the detail page does not, with
    nothing failing. `detail` is deliberately absent: it needs the Profile and
    CallRanking objects, which the list does not load (and loading them per row
    is the N+1 this task exists to avoid).

    Both callers' queries carry `.order_by(CallRankingCriterion.criterion)` --
    keep them in that same order (D5). Without it Postgres is free to hand back
    each surface's chips in a different sequence, and this shared function
    would not catch it.
    """
    return {
        "criterion": c.criterion,
        "label": criterion_label(c.criterion),
        "rating": c.rating,
        "is_topic_fit": c.criterion == "topic_fit",
        "rationale": c.rationale,
        "is_reason_code": c.criterion in _REASON_CODE_CRITERIA,
        "quoted_evidence": c.quoted_evidence,
    }


class CallListCriterionOut(BaseModel):
    """`CallDetailCriterionOut` minus `detail` -- see `_criterion_common_fields`.

    `rationale` and `quoted_evidence` are on the LIST schema, not only the
    detail page, because the card's chip expansion renders both (D10).
    """

    criterion: str
    label: str
    rating: str
    is_topic_fit: bool
    rationale: str | None
    is_reason_code: bool
    quoted_evidence: str | None


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
    # --- the match card's body (spec §5) ---------------------------------
    # Plain columns, all of them already loaded by the dashboard query:
    # `build_dashboard_view` defers ONLY `embedding`, `full_text` and `raw`
    # (with raiseload=True), so none of these costs an extra SELECT and none of
    # them trips the raiseload contract. `summary` is the one that can be long;
    # if the payload becomes a problem the lever is a `?fields=` projection --
    # do NOT re-defer `summary`, which reintroduces one SELECT per row (D10).
    programme: str | None
    budget_min: int | None
    budget_max: int | None
    currency: str | None
    url: str
    summary: str | None
    keywords: list[str]
    # Per-profile. Empty when no profile is selected or the call is unranked --
    # which is why the full edition's no-profile archive pays nothing for this.
    criteria: list[CallListCriterionOut]
    # "N things to check". `applicant_notes` is nullable and the local Gemma
    # path always leaves it NULL, so this is 0 far more often than not.
    applicant_note_count: int
    # Its OWN column, not `applicant_note_count > 0`: an escalation can succeed
    # and still carry zero notes when the analysis failed or was capped.
    escalated: bool


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


class CallDecisionOut(BaseModel):
    """The state the call is in AFTER the request. `null` is undecided.

    Undecided is the ABSENCE of a row, so the undo path returns `null` rather
    than inventing a fourth state name the table cannot store.

    NOT the same shape as `CallHideOut{hidden: bool}`, and deliberately so
    (D14): hide is a re-skin of an endpoint that predates the state column and
    keeps its wire contract; save is new and says which of the three states the
    call now sits in. The frontend has two hooks either way.
    """

    state: Literal["saved", "hidden"] | None


class CallDecisionCountsOut(BaseModel):
    inbox: int
    saved: int
    hidden: int


def _readable_active_profile_or_404(session, user: User, profile_id: int) -> None:
    """A decision needs a real, live, READABLE profile to attach to.

    `call_decisions.profile_id` is NOT NULL and FKs to `profiles`, so this is not
    defence in depth — it is the difference between a 404 and an IntegrityError.

    404 (not 403) for an unreadable id, matching `can_read_profile`'s contract:
    a profile the caller may not read must not be distinguishable from one that
    does not exist, or `?profile_id=N` becomes an existence oracle for every id
    a user can type.

    Deliberately NOT called by `unhide_call` or `list_hidden_calls` — see their
    docstrings; validating there would strand a user's own rows behind a 404.
    """
    live = session.scalar(
        select(Profile.id).where(Profile.id == profile_id, Profile.is_active.is_(True))
    )
    if live is None or not can_read_profile(session, user, profile_id):
        raise HTTPException(status_code=404, detail="profile not found")


@router.post("/calls/{call_id}/hide", response_model=CallHideOut)
def hide_call(
    request: Request,
    call_id: int,
    profile_id: int = Query(...),
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Hide one call FOR ONE PROFILE.

    `profile_id` is REQUIRED (422 without it). A decision row cannot exist
    without a profile, so there is no honest global hide left to offer — the
    full edition's unranked archive can no longer hide, and that is one of the
    two behaviours this task deliberately changes (D3).
    """
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        _readable_active_profile_or_404(session, user, profile_id)
        # DO UPDATE, not DO NOTHING. The row now carries a STATE: hiding a call
        # the user had saved must flip it, and DO NOTHING would silently leave
        # it saved while returning 200. All THREE columns move — `decided_at`
        # because this IS a new decision and the Saved/Hidden lists order by it
        # (D12), `decided_by` because it records who pressed the key.
        session.execute(
            pg_insert(CallDecision)
            .values(
                user_id=user.id,
                profile_id=profile_id,
                call_id=call_id,
                state="hidden",
                decided_by=user.id,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "profile_id", "call_id"],
                set_={"state": "hidden", "decided_at": func.now(), "decided_by": user.id},
            )
        )
        session.commit()
    return CallHideOut(hidden=True)


@router.delete("/calls/{call_id}/hide", response_model=CallHideOut)
def unhide_call(
    request: Request,
    call_id: int,
    profile_id: int = Query(...),
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Un-hide one call FOR ONE PROFILE so it reappears in that profile's feed.

    Scoped by user, by profile AND by state. `CallDecision.user_id == user.id`
    is this endpoint's ONLY authorization control — the unique key is
    (user_id, profile_id, call_id), so a colleague can hold their own hidden row
    on the very same call and profile, and dropping that clause would delete it.
    Deleting on (user_id, call_id) would reach across every other profile, and
    deleting without the state filter would throw away a SAVED decision on this
    profile — neither is an un-hide.

    `profile_id` is REQUIRED (422 without it, same as hide) but, unlike hide, is
    NOT run through `_readable_active_profile_or_404`. Every row this delete can
    touch is already scoped to `user_id == user.id`, so there is nothing left to
    leak by skipping the check — and skipping it is load-bearing: deactivate the
    profile, or have its sharing revoked, and the call the user hid on it must
    still be un-hideable, not stranded behind a 404 forever.

    Idempotent: 200 even when there was no hidden row to delete.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        session.execute(
            delete(CallDecision).where(
                CallDecision.user_id == user.id,
                CallDecision.profile_id == profile_id,
                CallDecision.call_id == call_id,
                CallDecision.state == "hidden",
            )
        )
        session.commit()
    return CallHideOut(hidden=False)


@router.post("/calls/{call_id}/save", response_model=CallDecisionOut)
def save_call(
    request: Request,
    call_id: int,
    profile_id: int = Query(...),
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Save one call FOR ONE PROFILE -- the genuinely new half of the pair.

    `profile_id` is REQUIRED (422 without it) and goes through the read rule,
    exactly as `hide_call` does: same table, same NOT NULL FK, same 404-not-403
    contract, so an id the caller may not read is indistinguishable from one
    that does not exist.

    DO UPDATE, not DO NOTHING. The first save of an undecided call INSERTS, and
    on a missing row DO UPDATE and DO NOTHING are indistinguishable -- the
    behaviour this chooses is only observable on the flip: saving a HIDDEN call
    must move it to saved, where DO NOTHING would answer 200 and leave it
    hidden. All THREE columns move: `decided_at` because this IS a new decision
    and Saved/Hidden are ordered by it (D12), `decided_by` because it records
    who pressed the key (D4).

    The conflict target is the full (user_id, profile_id, call_id) key, so this
    upsert can only ever touch the caller's own row -- a colleague holding their
    own decision on the same call and profile is untouched.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        _readable_active_profile_or_404(session, user, profile_id)
        session.execute(
            pg_insert(CallDecision)
            .values(
                user_id=user.id,
                profile_id=profile_id,
                call_id=call_id,
                state="saved",
                decided_by=user.id,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "profile_id", "call_id"],
                set_={"state": "saved", "decided_at": func.now(), "decided_by": user.id},
            )
        )
        session.commit()
    return CallDecisionOut(state="saved")


@router.delete("/calls/{call_id}/save", response_model=CallDecisionOut)
def unsave_call(
    request: Request,
    call_id: int,
    profile_id: int = Query(...),
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Un-save: back to undecided, which is the ABSENCE of a row.

    Scoped by user, by profile AND by state, for the same three reasons as
    `unhide_call`. `CallDecision.user_id == user.id` is this endpoint's ONLY
    authorization control -- the unique key is (user_id, profile_id, call_id),
    so a colleague can hold their own saved row on the very same call and
    profile and dropping that clause would delete it. Without the profile
    predicate the delete reaches across every other profile; without the state
    predicate an undo keypress on a HIDDEN card would throw the hide away too.

    `profile_id` is REQUIRED (422 without it, same as save) but, unlike save, is
    NOT run through `_readable_active_profile_or_404` (T2-10). Every row this
    delete can touch is already scoped to `user_id == user.id`, so there is
    nothing left to leak by skipping the check -- and skipping it is
    load-bearing: deactivate the profile, or have its sharing revoked, and the
    call the user saved on it must still be un-saveable, not stranded behind a
    404 forever.

    Idempotent: 200 even when there was no saved row to delete.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        if session.get(Call, call_id) is None:
            raise HTTPException(status_code=404, detail="call not found")
        session.execute(
            delete(CallDecision).where(
                CallDecision.user_id == user.id,
                CallDecision.profile_id == profile_id,
                CallDecision.call_id == call_id,
                CallDecision.state == "saved",
            )
        )
        session.commit()
    return CallDecisionOut(state=None)


@router.get("/calls/hidden", response_model=HiddenListOut)
def list_hidden_calls(
    request: Request,
    profile_id: int = Query(...),
    user: User = Depends(get_current_user),  # noqa: B008
):
    """The calls the current user has hidden ON ONE PROFILE, newest-hidden first.

    `profile_id` is REQUIRED (422 without it — D9). A decision belongs to a
    profile, so there is no honest cross-profile answer here; the alternative
    ("no profile = any profile, DISTINCT by call") costs a dedup subquery for a
    surface neither edition calls, and it is the one shape that WIDENS rather
    than narrows. No dedup, no union: one profile, one row per call under the
    unique key.

    NO readability check on the given profile_id, same reasoning as
    `unhide_call` above: every row is already scoped to `user.id`, so there is
    nothing to leak; adding one would only stop a user from seeing (and then
    un-hiding) their own rows after a profile was deactivated or un-shared.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        rows = session.execute(
            select(Call, CallDecision.decided_at)
            .join(
                CallDecision,
                (CallDecision.call_id == Call.id)
                & (CallDecision.user_id == user.id)
                & (CallDecision.profile_id == profile_id)
                & (CallDecision.state == "hidden"),
            )
            .options(joinedload(Call.source))
            .order_by(CallDecision.decided_at.desc(), Call.id.desc())
        ).all()
        items = [
            HiddenCallOut(
                id=call.id,
                title=call.title,
                deadline=call.deadline,
                deadline_local_date=deadline_local_date(call.deadline),
                source_name=(call.source.name if call.source else None),
            )
            for call, _decided_at in rows
        ]
    return HiddenListOut(items=items)


@router.get("/calls/decisions/counts", response_model=CallDecisionCountsOut)
def get_decision_counts(
    request: Request,
    profile_id: int = Query(...),
    source: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    show_closed: bool = Query(default=False),
    user: User = Depends(get_current_user),  # noqa: B008
):
    """Where inbox-zero comes from: how many calls sit in each triage view.

    Declared above `/calls/{call_id}`, but NOT because it has to be. MEASURED in
    Phase 2: Starlette compiles `/api/calls/{call_id}` to
    `^/api/calls/(?P<call_id>[^/]+)$`, and `[^/]+` cannot cross a slash, so this
    THREE-segment path never competes with it and is safe wherever it is
    declared. The order hazard is real for a TWO-segment path -- that same regex
    DOES match `/api/calls/counts` -- which is why `/calls/hidden` above must
    stay where it is, and why this path is three segments rather than two.

    `source`, `q` and `show_closed` are accepted with the LIST's names, defaults
    and semantics, so the rail and the feed agree under a filter and not only in
    the unfiltered case. `min_score` is deliberately NOT accepted (D18): a score
    floor needs the ranking join and `score_floor_active`'s three-way rule, and
    a second copy of that here is how the rail and the feed drift. It is also
    not applied, so a `min_score`-filtered list can show FEWER calls than these
    counts report for the same view; no release-1 surface can observe that (the
    match edition ships no `min_score` control).

    `profile_id` is REQUIRED and goes through the read rule, exactly as the two
    write routes do -- these counts are a per-profile fact about a user's own
    decisions, and an unreadable id must not be a probe.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        _readable_active_profile_or_404(session, user, profile_id)
        counts = count_decision_views(
            session,
            user_id=user.id,
            profile_id=profile_id,
            source=source,
            q=q,
            show_closed=show_closed,
        )
    return CallDecisionCountsOut(**counts)


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
        # This route resolves `?profile=` ITSELF — it never goes through
        # `dashboard.resolve_profile_selection` — so it composes the read rule
        # here. It used to resolve by is_active ALONE, which made it the
        # eleventh surface and the one an audit of "the profile endpoints"
        # walks straight past.
        #
        # Unreadable produces `profile_obj = None`, the SAME state an inactive
        # or unknown id produces, and NOT a 404: a status that separates
        # "exists but is not yours" from "does not exist" turns ?profile=N into
        # an oracle. `ranking` and `criteria` are populated only inside this
        # branch, so `profile_obj is None` is exactly the condition under which
        # no score, no criterion and no `_criterion_detail` string is rendered.
        if profile_id is not None:
            p = session.get(Profile, profile_id)
            if p is not None and p.is_active and can_read_profile(session, user, p.id):
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
                            select(CallRankingCriterion)
                            .where(
                                CallRankingCriterion.call_id == call_id,
                                CallRankingCriterion.profile_id == profile_id,
                            )
                            # D5: the same ORDER BY the list's batched query
                            # carries, so both surfaces agree on the sequence
                            # and not merely on the set.
                            .order_by(CallRankingCriterion.criterion)
                        ).scalars()
                    )

        # best-fit profile: highest-scoring profile OWNED by the current user.
        #
        # OWNERSHIP, not the read rule, and that is DELIBERATE — this line is
        # not a surface someone forgot to widen. Ownership is already narrower
        # than readability, so it leaks nothing. Widening it to
        # `readable_profile_ids` is defensible product-wise (a colleague a
        # profile is shared with would otherwise see it offered but never
        # chosen), but it is NOT a day-one no-op: every profile in the live
        # database is 'org', so a widened query would pick from every profile
        # in the organisation and change every user's call page the day it
        # merges. That is a product decision for a human, not part of the
        # privacy fix. Pinned by
        # tests/test_profile_access.py::test_best_profile_id_stays_owner_scoped_by_decision
        # — delete that gate in the commit that makes the decision.
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
                    criterion_label(ranking.aggregate_reason) if ranking.aggregate_reason else None
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
                **_criterion_common_fields(c),
                detail=(
                    _criterion_detail(c.criterion, call=call, profile=profile_obj, ranking=ranking)
                    if (profile_obj is not None and ranking is not None)
                    else None
                ),
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
    q: str | None = Query(default=None, max_length=200),
    min_score: int | None = Query(default=None, ge=0, le=100),
    # The four triage views over ONE query. `archive` is the default, so an
    # omitted parameter is byte-for-byte today's behaviour and the full edition
    # (which sends no `view`) cannot notice this exists. The Literal is written
    # out INLINE rather than reusing dashboard.View: ruff B008 exempts a
    # `Query()` default under an inline Literal but flags it under a
    # module-level alias — the same reason `sort` above duplicates its own.
    view: Literal["archive", "inbox", "saved", "hidden"] = Query(default="archive"),
    user: User = Depends(get_current_user),  # noqa: B008
):
    sf = request.app.state.session_factory
    with sf() as session:
        # READ-ONLY: persist=False — a deep link never changes the stored default.
        profile_id = resolve_profile_selection(
            session, user_id=user.id, raw_profile=profile, persist=False
        )
        # Named `dashboard`, not `view`: the `view` parameter is still needed
        # below (the criteria block reads `dashboard.effective`) and rebinding
        # it to the result would shadow the request's own triage view.
        dashboard = build_dashboard_view(
            session,
            user_id=user.id,
            source=source,
            sort=sort,
            profile_id=profile_id,
            limit=limit,
            page=page,
            show_closed=show_closed,
            q=q,
            min_score=min_score,
            view=view,
        )
        # Scope the chips to the profile the ranking join ACTUALLY used.
        # `build_dashboard_view` degrades an unknown/inactive id to None
        # internally and reports the result in `effective`; reading the local
        # `profile_id` instead would let one payload answer with a score from
        # no profile and chips from a suppressed one. `effective` is typed
        # dict[str, object], hence the isinstance narrowing rather than a cast.
        _eff = dashboard.effective["profile_id"]
        criteria_profile_id = _eff if isinstance(_eff, int) else None

        # ONE query for the whole page, not one per row. Reading
        # `call.rankings[i].criteria` inside the comprehension below would be a
        # lazy load per row -- invisible in a 1-row fixture and 50 round trips
        # on a full page. It runs INSIDE this `with sf() as session` block
        # because `dashboard.calls` holds instances attached to this session.
        # tests/web/test_calls_list_query_count.py is the gate.
        call_ids = [call.id for call, _ in dashboard.calls]
        criteria_by_call: dict[int, list[CallRankingCriterion]] = defaultdict(list)
        if criteria_profile_id is not None and call_ids:
            for crit in session.execute(
                select(CallRankingCriterion)
                .where(
                    CallRankingCriterion.profile_id == criteria_profile_id,
                    # The PAGE, not the profile. Without this the query reads
                    # the profile's entire criteria table on every request and
                    # discards everything off-page -- one statement, identical
                    # response, unbounded read.
                    CallRankingCriterion.call_id.in_(call_ids),
                )
                # D5: deterministic, and the same ORDER BY the detail query got.
                .order_by(CallRankingCriterion.criterion)
            ).scalars():
                criteria_by_call[crit.call_id].append(crit)

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
                programme=call.programme,
                budget_min=call.budget_min,
                budget_max=call.budget_max,
                currency=call.currency,
                url=call.url,
                summary=call.summary,
                keywords=list(call.keywords or []),
                criteria=[
                    CallListCriterionOut(**_criterion_common_fields(crit))
                    for crit in criteria_by_call[call.id]
                ],
                # `ranking` is Optional and None is the DEFAULT list state, not
                # an edge case: the no-profile view has no rankings at all, and
                # even a ranked view LEFT JOINs, so a newly ingested call has
                # none. `len(ranking.applicant_notes or [])` without the guard
                # is an AttributeError on the commonest row there is.
                applicant_note_count=(0 if ranking is None else len(ranking.applicant_notes or [])),
                escalated=bool(ranking is not None and ranking.escalated),
            )
            for call, ranking in dashboard.calls
        ]
        facets = FacetsOut(
            sources=[SourceFacet(slug=s.slug, name=s.name) for s in dashboard.sources],
            profiles=[ProfileFacet(id=p.id, name=p.name) for p in dashboard.profiles],
        )
    return DashboardOut(
        items=items,
        summary=dashboard.summary,
        pagination=dashboard.pagination,
        ranking=dashboard.ranking,
        effective=dashboard.effective,
        facets=facets,
    )
