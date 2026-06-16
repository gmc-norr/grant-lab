"""Read-only dashboard read-model service.

Originally extracted from the (now-retired) `web/routes/calls.py::calls_index`
Jinja handler so the same query/ordering/summary logic could be shared. After
Plan 3 Task 9 flipped `/` to the React SPA and deleted `calls_index`, the sole
live consumer is the JSON `GET /api/calls` endpoint (`web/api/calls.py`), which
always calls with `persist=False` and never writes. The explicit sticky-default
WRITE that used to live in the Jinja handler now lives in
`POST /api/me/default-profile` (via `set_default_profile`).

PARITY CONTRACT: this module still reproduces the legacy `calls_index` read
behavior byte-for-byte — same closed-first tie-break ordering, the same
dismissed/`show_closed` hide logic, the same summary counts over the full
filtered set (independent of limit/offset), and the same pagination flags. That
behavior is now proven against `/api/calls` in `tests/test_api_calls_feed_behavior.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from call_manager.db.models import (
    Call,
    CallRanking,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
    UserCallDismissal,
)

# Per design spec: "Sort: score, deadline, newest." Score is the per-profile
# ranked branch; for the unranked branch we offer the two ordering primitives
# that don't depend on rankings.
Sort = Literal["newest", "deadline"]


@dataclass
class DashboardView:
    calls: list[tuple[Call, CallRanking | None]]
    sources: list[Source]
    profiles: list[Profile]
    summary: dict[str, int]
    pagination: dict[str, object]
    ranking: dict[str, int]
    effective: dict[str, object]
    now: datetime


def resolve_profile_selection(
    session: Session, *, user_id: int, raw_profile: str | None, persist: bool
) -> int | None:
    """Resolve the `?profile` query param into a valid active profile id.

    Mirrors the legacy three-way sticky-default resolution from `calls_index`:

    - absent (``None``): honour the user's stored default if still active, else
      unranked. Never writes.
    - explicit empty (``""``): unranked. When ``persist`` is True, also CLEAR
      the stored default.
    - an id was given: validate (must exist AND be active) BEFORE persisting.
      Invalid/inactive → unranked, stored default untouched. When ``persist`` is
      True and the id is valid, store it as the new default.

    ``persist=False`` is the read-only `/api/calls` path (the only live caller):
    identical resolution, never writes. The ``persist=True`` write branch is
    currently UNUSED — it backed the retired Jinja `calls_index` handler; explicit
    preference writes now go through `POST /api/me/default-profile`
    (`set_default_profile`). The branch is retained (and the *caller* is still
    responsible for committing) against a future server-side resolve-and-persist
    need.

    A non-empty, non-integer value 400s, matching the legacy
    `_parse_optional_profile_id` behavior (a genuinely malformed URL).
    """
    if raw_profile is None:
        # Bare / or return visit — honour stored default if still active.
        stored = session.scalar(select(User.default_profile_id).where(User.id == user_id))
        if stored is None:
            return None
        return session.scalar(
            select(Profile.id).where(Profile.id == stored, Profile.is_active.is_(True))
        )

    if raw_profile == "":
        # Explicit "— No profile —": clear stored default (Jinja path only).
        # Branch on the UNTRIMMED value for byte-for-byte legacy parity: legacy
        # `calls_index` cleared on `elif raw_profile == ""`. A whitespace-only
        # value ("%20") is NOT exactly "", so it never reached the clear branch —
        # it fell through to the id branch, where _parse_optional_profile_id
        # stripped it to "" and returned None, so the validation lookup found
        # nothing and the stored default was left untouched. Reproduced below.
        if persist:
            u = session.get(User, user_id)
            if u is not None and u.default_profile_id is not None:
                u.default_profile_id = None
        return None

    # Strip only here, immediately before the parse attempt. A whitespace-only
    # value strips to "" → treated as "no id requested" (requested=None) →
    # resolves to None without writing, exactly as legacy did (no 400). Only a
    # NON-EMPTY, non-integer value 400s (a genuinely malformed URL).
    raw = raw_profile.strip()
    if raw == "":
        requested: int | None = None
    else:
        try:
            requested = int(raw)
        except ValueError as e:
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail=f"invalid profile id: {raw!r}") from e

    # Validate BEFORE persisting — invalid/inactive leaves the stored default alone.
    valid = session.scalar(
        select(Profile.id).where(Profile.id == requested, Profile.is_active.is_(True))
    )
    if valid is not None and persist:
        u = session.get(User, user_id)
        if u is not None and u.default_profile_id != valid:
            u.default_profile_id = valid
    return valid


def set_default_profile(session: Session, *, user_id: int, profile_id: int | None) -> None:
    """Explicit preference write for ``POST /api/me/default-profile``.

    ``None`` clears the stored default. A non-None id must reference an existing
    active profile or this 404s. The caller commits.
    """
    u = session.get(User, user_id)
    if u is None:
        return
    if profile_id is None:
        u.default_profile_id = None
        return
    valid = session.scalar(
        select(Profile.id).where(Profile.id == profile_id, Profile.is_active.is_(True))
    )
    if valid is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="profile not found or inactive")
    u.default_profile_id = valid


def _order_for(sort: Sort):
    if sort == "deadline":
        # Soonest deadline first; never-expires (NULL) sink to the bottom.
        return (Call.deadline.asc().nullslast(), Call.id.desc())
    # "newest" (default): most recently published first; older or unknown
    # publication date sink to the bottom.
    return (Call.published_at.desc().nullslast(), Call.id.desc())


def _ranking_counts(session: Session, profile_id: int) -> tuple[int, int]:
    """(pending, unresolved-failed) job counts for a profile.

    Used by `build_dashboard_view` to populate the `ranking` block that the
    React SPA renders from `GET /api/calls`. PARITY: identical to the legacy
    local `_ranking_counts` that backed the retired Jinja dashboard.
    """
    pending = (
        session.scalar(
            select(func.count())
            .select_from(RankingJob)
            .where(
                RankingJob.profile_id == profile_id,
                RankingJob.status.in_((RankingJobStatus.PENDING, RankingJobStatus.RUNNING)),
            )
        )
        or 0
    )
    # Unresolved failures only: a FAILED job with no CallRanking yet for that pair.
    scored = select(CallRanking.call_id).where(
        CallRanking.profile_id == profile_id, CallRanking.call_id == RankingJob.call_id
    )
    failed = (
        session.scalar(
            select(func.count())
            .select_from(RankingJob)
            .where(
                RankingJob.profile_id == profile_id,
                RankingJob.status == RankingJobStatus.FAILED,
                ~scored.exists(),
            )
        )
        or 0
    )
    return pending, failed


def build_dashboard_view(
    session: Session,
    *,
    user_id: int,
    source: str | None,
    sort: Sort,
    profile_id: int | None,
    limit: int,
    page: int,
    show_closed: bool,
) -> DashboardView:
    """Build the dashboard read-model (read-only; no writes).

    Reproduces the legacy `calls_index` read path exactly. The caller passes the
    already-resolved `profile_id` (see `resolve_profile_selection`); an
    unknown/inactive id degrades to the unranked view, and an unknown `source`
    slug degrades to "all sources" (selection cleared).
    """
    now = datetime.now(UTC)
    soon_cutoff = now + timedelta(days=30)
    # "Closed" is derived from the deadline — no status column. A call with no
    # deadline (rolling) is open. Same comparison used in filter, sort, badges.
    closed_expr = and_(Call.deadline.isnot(None), Call.deadline <= now)
    open_expr = or_(Call.deadline.is_(None), Call.deadline > now)
    offset = (page - 1) * limit

    # Resolve the source slug to an id once. An unknown/stale slug degrades to
    # "all sources" (selection cleared) rather than a confusing empty feed with
    # the bad slug still selected.
    source_id: int | None = None
    selected_source = source
    if source:
        source_id = session.scalar(select(Source.id).where(Source.slug == source))
        if source_id is None:
            selected_source = None

    # Load the profile object when we have a valid id (for the ranked view and
    # downstream metric queries). An inactive/unknown id degrades to unranked.
    profile_obj: Profile | None = None
    if profile_id is not None:
        profile_obj = session.execute(
            select(Profile).where(Profile.id == profile_id, Profile.is_active.is_(True))
        ).scalar_one_or_none()
        if profile_obj is None:
            profile_id = None

    # When a profile is selected, LEFT JOIN call_rankings for THAT profile and
    # sort by score desc (nulls last). Without a profile, fall back to the
    # unranked sort (newest/deadline).
    if profile_id is not None and profile_obj is not None:
        stmt = (
            select(Call, CallRanking)
            .options(joinedload(Call.source))
            .outerjoin(
                CallRanking,
                (CallRanking.call_id == Call.id) & (CallRanking.profile_id == profile_id),
            )
            .order_by(
                closed_expr.asc(),  # open (False=0) before closed (True=1)
                CallRanking.aggregate_score.desc().nullslast(),
                CallRanking.embed_similarity.desc().nullslast(),
                Call.published_at.desc().nullslast(),
                Call.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    else:
        stmt = (
            select(Call, None)
            .options(joinedload(Call.source))
            .order_by(closed_expr.asc(), *_order_for(sort))
            .limit(limit)
            .offset(offset)
        )

    dismissed_subq = (
        select(UserCallDismissal.call_id)
        .where(UserCallDismissal.user_id == user_id)
        .scalar_subquery()
    )
    # Filters shared by the page query and the summary aggregates, so the badges
    # reflect the whole filtered dataset — not just the page.
    base_filters = [Call.id.notin_(dismissed_subq)]
    if source_id is not None:
        base_filters.append(Call.source_id == source_id)
    # The list (and its pagination total) hide closed calls by default.
    list_filters = list(base_filters)
    if not show_closed:
        list_filters.append(open_expr)
    rows = session.execute(stmt.where(*list_filters)).unique().all()
    # Normalize to (Call, CallRanking|None) shape regardless of branch.
    calls: list[tuple[Call, CallRanking | None]] = [
        (row[0], row[1] if profile_id is not None else None) for row in rows
    ]

    # Summary metrics computed with aggregates over the full filtered set
    # (independent of limit/offset), so a corpus larger than one page can't
    # undercount the badges.

    # Pagination total tracks the visible (toggled) set.
    list_total = session.scalar(select(func.count()).select_from(Call).where(*list_filters)) or 0
    # "Open calls" badge is always the genuine open count, toggle or not.
    open_count = (
        session.scalar(select(func.count()).select_from(Call).where(*base_filters, open_expr)) or 0
    )
    soon_deadline = (
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(
                *base_filters,
                Call.deadline.isnot(None),
                Call.deadline > now,
                Call.deadline <= soon_cutoff,
            )
        )
        or 0
    )
    high_score = 0
    if profile_id is not None and profile_obj is not None:
        high_score = (
            session.scalar(
                select(func.count())
                .select_from(CallRanking)
                .join(Call, Call.id == CallRanking.call_id)
                .where(
                    *base_filters,
                    open_expr,  # a closed call is never a "top match"
                    CallRanking.profile_id == profile_id,
                    CallRanking.aggregate_score >= 70,
                )
            )
            or 0
        )

    sources = session.execute(select(Source).order_by(Source.name)).scalars().all()
    profiles = (
        session.execute(select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.name))
        .scalars()
        .all()
    )

    pending, failed = _ranking_counts(session, profile_id) if profile_id is not None else (0, 0)

    return DashboardView(
        calls=calls,
        sources=sources,
        profiles=profiles,
        summary={
            "open_count": open_count,
            "high_score": high_score,
            "soon_deadline": soon_deadline,
        },
        pagination={
            "page": page,
            "limit": limit,
            "has_prev": page > 1,
            "has_next": offset + len(calls) < list_total,
            "total": list_total,
        },
        ranking={"pending": pending, "failed": failed},
        effective={
            "source": selected_source,
            "sort": sort,
            "profile_id": profile_id,
            "show_closed": show_closed,
        },
        now=now,
    )
