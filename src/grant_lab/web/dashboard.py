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

from grant_lab.db.models import (
    Call,
    CallDecision,
    CallRanking,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from grant_lab.db.projections import call_columns_no_response_renders
from grant_lab.web.profile_access import can_read_profile, readable_profile_ids

# Score threshold for counting "high match" calls in the dashboard summary.
# Calls with aggregate_score >= this value are highlighted as top matches.
# NOTE: full-sort is O(n) — if the dataset grows beyond ~10k calls, consider
# replacing the sort with a pagination-aware approach or a partial index scan.
DASHBOARD_HIGH_SCORE_THRESHOLD = 70

# Per design spec: "Sort: score, deadline, newest." Score is the per-profile
# ranked branch; for the unranked branch we offer the two ordering primitives
# that don't depend on rankings.
Sort = Literal["newest", "deadline"]

# The four views of ONE query. Everything else -- source, q, show_closed, the
# ranking join, pagination -- is shared, so a saved call renders with the SAME
# card as an inbox call and there is no second endpoint to keep in sync.
#
# 'archive' is the default so an omitted parameter is exactly today's behaviour:
# a client that sends no `view` gets the archive.
#
# NOT re-used as the type of the HTTP query parameter in web/api/calls.py: ruff
# B008 exempts a `Query()` default under an INLINE Literal but flags it under a
# module-level alias, which is why the existing `sort` parameter duplicates its
# Literal inline too.
View = Literal["archive", "inbox", "saved", "hidden"]


def _decision_predicate(view: View, *, user_id: int, profile_id: int | None):
    """The ONLY thing that differs between the four views' row sets.

    profile_id is None -> archive/inbox: the unranked archive,
    suppressing a call hidden on ANY of the user's profiles (today's behaviour
    verbatim; NOT "hidden on every profile", a different query that would
    re-surface calls the user has dealt with). For saved/hidden with no profile
    there is nothing to scope a decision to, and the ANY rule would WIDEN --
    so those views are EMPTY (Phase 2 decision D8), never the cross-profile
    union. The match frontend still needs a 200 with `effective.profile_id:
    null` to branch to its onboarding screen, which is why this is an empty
    page rather than a 422.

    CALLERS. `_base_call_filters` (:329) and, from OUTSIDE this module,
    `grant_lab.boards.queue.board_queue`. The board queue is the odd one: it
    passes the PROFILE OWNER's user_id rather than the requesting user's, which
    is exactly why it CALLS this instead of forking it (2026-09-05 decisions
    note D2). A second copy is how the rail and the feed drift apart under a
    filter nobody re-tested.
    """
    rows = select(CallDecision.call_id).where(CallDecision.user_id == user_id)
    if profile_id is not None:
        rows = rows.where(CallDecision.profile_id == profile_id)
    if view == "inbox":
        return Call.id.notin_(rows.scalar_subquery())
    if view in ("saved", "hidden"):
        if profile_id is None:
            return Call.id.in_([])  # empty, never the union
        return Call.id.in_(rows.where(CallDecision.state == view).scalar_subquery())
    if view != "archive":
        # The HTTP Literal is the guard for the route; this is the tripwire for
        # a caller INSIDE the package that invents a fifth name -- without it a
        # typo silently gets the archive.
        raise ValueError(f"unknown view {view!r}")
    return Call.id.notin_(rows.where(CallDecision.state == "hidden").scalar_subquery())


@dataclass
class DashboardView:
    # Three-tuple since 2026-09-07: the third slot is the user's own decision
    # note, non-None only on the saved/hidden views (spec §4).
    calls: list[tuple[Call, CallRanking | None, str | None]]
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

    - absent (``None``): honour the user's stored default if still active AND
      still readable, else unranked. Never writes.
    - explicit empty (``""``): unranked. When ``persist`` is True, also CLEAR
      the stored default.
    - an id was given: validate (must exist, be active AND be readable by this
      user) BEFORE persisting. Invalid/inactive/unreadable → unranked, stored
      default untouched. When ``persist`` is True and the id is valid, store it
      as the new default.

    BOTH id-bearing arms compose ``profile_access.can_read_profile``, and they
    give unreadable ids the SAME answer as unknown ones — see the comments at
    each. The user is loaded here because the live callers hold only a
    ``user_id``; a user id that no longer resolves gets the unranked view.

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
        # Bare / or return visit — honour stored default if still active AND
        # still readable. THIS ARM IS THE ONE A FIX MISSES: no `?profile=` is
        # sent at all, so a check placed only on the explicit arm below never
        # runs, and a default stored while a profile was shared keeps ranking
        # the feed forever after the share is revoked.
        stored = session.scalar(select(User.default_profile_id).where(User.id == user_id))
        if stored is None:
            return None
        viewer = session.get(User, user_id)
        if viewer is None or not can_read_profile(session, viewer, stored):
            # Degrade to unranked, exactly as a deactivated default does. The
            # users.default_profile_id row is deliberately LEFT ALONE:
            # revocation is a FILTER, not a delete, so re-sharing restores the
            # colleague's preference without them setting it again.
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
    if valid is not None:
        # And unreadable degrades to unranked, with the SAME answer an unknown
        # or inactive id gets. It must NOT 404: a status that distinguishes
        # "exists but is not yours" from "does not exist" makes ?profile=N an
        # oracle for every id a user can type, and profile NAMES are the thing
        # worth not confirming.
        viewer = session.get(User, user_id)
        if viewer is None or not can_read_profile(session, viewer, valid):
            return None
    if valid is not None and persist:
        u = session.get(User, user_id)
        if u is not None and u.default_profile_id != valid:
            u.default_profile_id = valid
    return valid


def set_default_profile(session: Session, *, user_id: int, profile_id: int | None) -> None:
    """Explicit preference write for ``POST /api/me/default-profile``.

    ``None`` clears the stored default. A non-None id must reference an existing
    active profile THIS USER MAY READ or this 404s. The caller commits.

    Without the read check this endpoint is the bypass: store an unreadable
    profile as the default, then load a bare ``/api/calls`` with no ``?profile=``
    at all. ONE status — the same 404 — for missing, inactive and unreadable, so
    storing is not an existence oracle either.
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
    if valid is None or not can_read_profile(session, u, valid):
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
    """(pending jobs, unscorable OPEN CALLS) for a profile.

    Used by `build_dashboard_view` to populate the `ranking` block that the
    React SPA renders from `GET /api/calls` ("Scoring N calls…" / "N calls
    couldn't be scored automatically").

    THE SECOND NUMBER IS CALLS, NOT ROWS, AND OPEN CALLS ONLY (review
    2026-10-26 M-core-2). It used to be a `count(*)` over FAILED `ranking_jobs`
    with no deadline filter, and `ranking_jobs` keeps every attempt forever: the
    nightly reconcile re-enqueues the same pair about twice a night, so one call
    that cannot be scored added about 2 to the banner every day and a call whose
    deadline passed years ago kept contributing. Live, profile 1 read 97 over 9
    distinct calls, only 2 of them open — the sentence named a quantity of
    research opportunity and reported a quantity of database rows.

    PARITY NOTE: the legacy Jinja `_ranking_counts` this was extracted from had
    the same bug. The parity contract at the top of this module is about the
    LIST query, ordering and pagination; it is not a licence to keep a count
    wrong, and nothing renders the Jinja dashboard any more.

    `pending` is deliberately left as a row count: `uq_ranking_jobs_active_pair`
    is unique on (call_id, profile_id) WHERE status IN ('pending','running'), so
    rows and pairs are the same number there by construction.
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
            select(func.count(func.distinct(RankingJob.call_id)))
            .select_from(RankingJob)
            .join(Call, Call.id == RankingJob.call_id)
            .where(
                RankingJob.profile_id == profile_id,
                RankingJob.status == RankingJobStatus.FAILED,
                # Same "closed is derived from the deadline" rule
                # `build_dashboard_view` applies; NOT gated on `show_closed`,
                # because the sentence is about work the reader can still do and
                # a closed call is not work.
                or_(Call.deadline.is_(None), Call.deadline > datetime.now(UTC)),
                ~scored.exists(),
            )
        )
        or 0
    )
    return pending, failed


# Which statuses count as an ATTEMPT THAT FINISHED. `mark_failed(terminal=False)`
# puts a job back to PENDING, so FAILED is only ever the dead-letter after
# MAX_ATTEMPTS — "the model gave up on this pair", not "one request hiccuped".
_TERMINAL_JOB_STATUSES = (RankingJobStatus.DONE, RankingJobStatus.FAILED)


def latest_failed_ranking_attempts(
    session: Session, *, profile_id: int, call_ids: list[int]
) -> dict[int, datetime]:
    """{call_id: when it last failed} for the pairs whose LATEST attempt failed.

    Answers "could we rank this call at all?" for the surfaces that show a
    score, so a ranking from before the 2026-10-06 Ollama upgrade stops
    presenting itself as current (review 2026-10-26 A4 finding 2: 13 open calls
    were doing exactly that, invisibly, because `_ranking_counts` above counts
    only pairs with NO ranking at all).

    COMPUTED, NOT STORED — there is no migration here. `ranking_jobs` already
    records every attempt; this is one window function over it.

    "LATEST" is defined over TERMINAL rows only (`_TERMINAL_JOB_STATUSES`),
    ordered by `updated_at DESC, id DESC`. Three decisions in that sentence:

    * TERMINAL ONLY. `enqueue_for_pair` stamps `updated_at = now()`, so taking
      the newest row of any status would clear the mark the moment the 04:00
      reconcile queued a retry and restore it when that retry dead-lettered
      hours later — a marker that blinks with the scheduler instead of with the
      facts.
    * LATEST, not EXISTS. The table is append-only in practice (nothing deletes
      a finished job), so `EXISTS (status='failed')` would mark every pair that
      ever had a bad night, which is most of them.
    * `id DESC` BREAKS TIES. Two attempts in the same transaction, or a backfill
      that stamped a batch with one timestamp, otherwise leave the winner to the
      planner — and `id` is a monotone BigInteger sequence, so it is the real
      arrival order.

    Returns an EMPTY dict for an empty `call_ids` (the no-profile archive and an
    empty page both land here) without issuing a statement. Batched on purpose:
    ONE query for the whole page, the same rule
    `tests/web/test_calls_list_query_count.py` pins for the criterion chips.
    """
    if not call_ids:
        return {}
    attempts = (
        select(
            RankingJob.call_id.label("call_id"),
            RankingJob.status.label("status"),
            RankingJob.updated_at.label("updated_at"),
            func.row_number()
            .over(
                partition_by=RankingJob.call_id,
                order_by=(RankingJob.updated_at.desc(), RankingJob.id.desc()),
            )
            .label("rn"),
        )
        .where(
            RankingJob.profile_id == profile_id,
            RankingJob.status.in_(_TERMINAL_JOB_STATUSES),
            RankingJob.call_id.in_(call_ids),
        )
        .subquery()
    )
    rows = session.execute(
        select(attempts.c.call_id, attempts.c.updated_at).where(
            attempts.c.rn == 1,
            attempts.c.status == RankingJobStatus.FAILED,
        )
    ).all()
    return {call_id: failed_at for call_id, failed_at in rows}


def _ilike_escape(term: str) -> str:
    """Escape LIKE wildcards so a user search term matches literally. ``\\`` is
    the escape char passed to ``.ilike(..., escape="\\\\")``."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_predicate(q: str):
    """Substring free-text match: AND across whitespace-split terms, OR across
    title/summary/full_text/keywords. Index-backed (pg_trgm) on the first three;
    the keywords leg is an accepted unindexed expression scan."""
    keywords_str = func.array_to_string(Call.keywords, " ")
    term_clauses = []
    for term in q.split():
        pat = f"%{_ilike_escape(term)}%"
        term_clauses.append(
            or_(
                Call.title.ilike(pat, escape="\\"),
                Call.summary.ilike(pat, escape="\\"),
                Call.full_text.ilike(pat, escape="\\"),
                keywords_str.ilike(pat, escape="\\"),
            )
        )
    return and_(*term_clauses)


def _base_call_filters(
    view: View, *, user_id: int, profile_id: int | None, source_id: int | None, q: str | None
):
    """The ONE definition of the call-matching filters, shared by the list, its
    badges and the rail counts.

    One definition, so the rail cannot say "3" over a feed showing 2. The `view`
    predicate is the only per-view part; `source` and free text restrict the same
    corpus in every view. `build_dashboard_view` and `count_decision_views` are
    the only callers and MUST NOT grow separate copies of this -- a second copy
    is how the rail and the feed drift apart under a filter nobody re-tested.

    `open_expr` is deliberately NOT here. It is applied by the caller, because it
    is applied CONDITIONALLY and per view: `show_closed` governs inbox/archive
    and never saved/hidden (D1). Folding it in would force both callers to undo
    it for two of the four views.
    """
    filters = [_decision_predicate(view, user_id=user_id, profile_id=profile_id)]
    if source_id is not None:
        filters.append(Call.source_id == source_id)
    if q and q.strip():
        filters.append(_search_predicate(q.strip()))
    return filters


def count_decision_views(
    session: Session,
    *,
    user_id: int,
    profile_id: int,
    source: str | None = None,
    q: str | None = None,
    show_closed: bool = False,
) -> dict[str, int]:
    """How many calls sit in each triage view, for ONE profile. Inbox-zero.

    THREE calls to `_base_call_filters`, one per view -- not one shared "the list
    predicate" call. No single predicate is both: the inbox excludes every
    decision, saved and hidden select one state each.

    Takes the list's filters, with the LIST'S NAMES, defaults and semantics --
    `source`, `q`, `show_closed` -- so the rail and the feed agree under a filter
    and not only in the unfiltered case. `open_expr` is applied exactly as the
    list applies it: to `inbox` unless `show_closed`, and NEVER to `saved` or
    `hidden`, which list DECISIONS and keep a call that has closed (D1).

    `min_score` is deliberately NOT accepted (D18). A score floor needs the
    ranking join and `score_floor_active`'s three-way rule, and duplicating that
    here is exactly how the rail and the feed drift; no release-1 surface can
    observe the difference (no screen offers a `min_score` control). A
    `min_score`-filtered list can therefore show FEWER calls than these counts
    report for the same view.

    `profile_id` is REQUIRED, unlike the list's optional `?profile`. There is no
    honest "counts for the unranked archive": the archive is one view, not three,
    and saved/hidden with no profile are empty by D8.
    """
    open_expr = or_(Call.deadline.is_(None), Call.deadline > datetime.now(UTC))
    # Unknown/stale slug degrades to "all sources", exactly as the list does.
    source_id = session.scalar(select(Source.id).where(Source.slug == source)) if source else None

    def count(view: View) -> int:
        filters = _base_call_filters(
            view, user_id=user_id, profile_id=profile_id, source_id=source_id, q=q
        )
        if not show_closed and view not in ("saved", "hidden"):
            filters.append(open_expr)
        return session.scalar(select(func.count()).select_from(Call).where(*filters)) or 0

    return {"inbox": count("inbox"), "saved": count("saved"), "hidden": count("hidden")}


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
    q: str | None = None,
    min_score: int | None = None,
    view: View = "archive",
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

    # THREE columns, and this list is the whole of what a dashboard row must not
    # load: `embedding`, `full_text` and `raw`. `CallOut` (web/api/calls.py)
    # renders 18 fields and none of them is one of these — the match card's body
    # (programme, budget, url, `summary`, keywords) is plain columns the query
    # already loads, so widening the response does NOT mean widening this tuple.
    # Selecting the three anyway made each row ship a 1024-dim vector as ~9 kB of
    # text for pgvector's PURE-PYTHON parser to turn back into 1024 floats —
    # measured at 71% (50 rows) and 78% (200 rows) of the whole ORM row-load, on
    # data the response never contains.
    #
    # The list may only ever SHRINK toward those three. In particular `summary`
    # must NOT be added back (D10): it is rendered on every card, so deferring it
    # buys nothing and costs one extra SELECT per row — an N+1 where the payload
    # lever is a `?fields=` projection instead.
    #
    # `raiseload=True` makes this a contract, not a hope: a consumer that reads
    # one of these off a dashboard row raises instead of silently issuing one
    # extra SELECT per row. `build_dashboard_view` has exactly one production
    # caller (`/api/calls`), so widening this is a deliberate act — drop the
    # column from the tuple, do not catch the error.
    # ONE home for this list (db/projections.py). The board queue and the public
    # board wall defer exactly the same three columns for exactly the same
    # reason, and three copies of three column names is how they drift --
    # silently, because a query that stops deferring gets slower and stays
    # correct, so nothing goes red. The reasoning above is why THIS list; the
    # shared function is what stops it becoming three lists.
    _unrendered = call_columns_no_response_renders()

    # When a profile is selected, LEFT JOIN call_rankings for THAT profile and
    # sort by score desc (nulls last). Without a profile, fall back to the
    # unranked sort (newest/deadline).
    # Saved and Hidden are DECISION views -- a log of what the user just did --
    # so they are ordered most-recent-decision-first (D12), not by score. The
    # join IS the EXISTS: (user_id, profile_id, call_id) is unique, so it can
    # match at most one row per call, and it is what puts `decided_at` in scope
    # for the ORDER BY. It cannot fire without a profile: `_decision_predicate`
    # returns the empty set for saved/hidden when profile_id is None (D8), and
    # profile_id is None there is exactly the branch below.
    decision_ordered = view in ("saved", "hidden") and profile_id is not None
    if profile_id is not None and profile_obj is not None:
        stmt = (
            select(Call, CallRanking)
            .options(joinedload(Call.source), *_unrendered)
            .outerjoin(
                CallRanking,
                (CallRanking.call_id == Call.id) & (CallRanking.profile_id == profile_id),
            )
        )
        if decision_ordered:
            stmt = (
                stmt.join(
                    CallDecision,
                    (CallDecision.call_id == Call.id)
                    & (CallDecision.user_id == user_id)
                    & (CallDecision.profile_id == profile_id)
                    & (CallDecision.state == view),
                )
                # The row is ALREADY here, joined for the ORDER BY below. The
                # note is one more column off it -- no new FROM entry, no
                # subselect, no extra statement. Conditional, so inbox and
                # archive (which do not join) pay nothing and cannot
                # accidentally project it.
                .add_columns(CallDecision.note)
                .order_by(CallDecision.decided_at.desc(), Call.id.desc())
            )
        else:
            stmt = stmt.order_by(
                closed_expr.asc(),  # open (False=0) before closed (True=1)
                CallRanking.aggregate_score.desc().nullslast(),
                CallRanking.embed_similarity.desc().nullslast(),
                Call.published_at.desc().nullslast(),
                Call.id.desc(),
            )
        if sort == "deadline":
            stmt = stmt.order_by(None).order_by(closed_expr.asc(), *_order_for(sort))
        stmt = stmt.limit(limit).offset(offset)
    else:
        stmt = (
            select(Call, None)
            .options(joinedload(Call.source), *_unrendered)
            .order_by(closed_expr.asc(), *_order_for(sort))
            .limit(limit)
            .offset(offset)
        )

    # Filters shared by the page query and the summary aggregates, so the badges
    # reflect the whole filtered dataset — not just the page. `profile_id` here
    # is the EFFECTIVE one: an unknown/inactive/unreadable id was degraded to
    # None above, so the decision scope and the ranking join always agree about
    # which profile this is.
    base_filters = _base_call_filters(
        view, user_id=user_id, profile_id=profile_id, source_id=source_id, q=q
    )
    # The list (and its pagination total) hide closed calls by default -- but
    # NOT in the Saved and Hidden views (D1). Those list DECISIONS: a researcher
    # who saved a call because they intend to apply must not watch it vanish
    # exactly when they go looking for it, and a hidden call that closed is
    # still a hidden call. `show_closed` therefore affects only inbox/archive.
    list_filters = list(base_filters)
    if not show_closed and view not in ("saved", "hidden"):
        list_filters.append(open_expr)
    # Score floor: ranked branch only (unranked calls have no score). The ranked
    # `stmt` already has CallRanking in its FROM, so the predicate resolves there;
    # because `>= min_score` is false for the LEFT-JOIN NULLs, unranked calls drop
    # out — the intended "matched calls only" behaviour.
    score_floor_active = (
        profile_id is not None and profile_obj is not None and min_score is not None
    )
    list_where = list(list_filters)
    if score_floor_active:
        list_where.append(CallRanking.aggregate_score >= min_score)
    rows = session.execute(stmt.where(*list_where)).unique().all()
    # Normalize to (Call, CallRanking|None, note|None) shape regardless of
    # branch. Only the decision_ordered branch added a third column, and
    # `decision_ordered` is true exactly when that branch ran -- so indexing
    # row[2] anywhere else would be an IndexError, not a None.
    calls: list[tuple[Call, CallRanking | None, str | None]] = [
        (
            row[0],
            row[1] if profile_id is not None else None,
            row[2] if decision_ordered else None,
        )
        for row in rows
    ]

    # Summary metrics computed with aggregates over the full filtered set
    # (independent of limit/offset), so a corpus larger than one page can't
    # undercount the badges.

    # Pagination total tracks the visible (toggled) set — including the score
    # floor, so total/has_next stay correct. A floored count must JOIN CallRanking
    # (a plain Call count can't see aggregate_score).
    if score_floor_active:
        list_total = (
            session.scalar(
                select(func.count())
                .select_from(Call)
                .outerjoin(
                    CallRanking,
                    (CallRanking.call_id == Call.id) & (CallRanking.profile_id == profile_id),
                )
                .where(*list_where)
            )
            or 0
        )
    else:
        list_total = (
            session.scalar(select(func.count()).select_from(Call).where(*list_filters)) or 0
        )
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
                    CallRanking.aggregate_score >= DASHBOARD_HIGH_SCORE_THRESHOLD,
                )
            )
            or 0
        )

    sources = session.execute(select(Source).order_by(Source.name)).scalars().all()
    # The facet list is the profile picker, so its payload is profile NAMES —
    # offered to everyone before this filter existed. A name is the leak worth
    # closing: a dropdown of every private profile is a directory of who is
    # working on what. Same composition the profiles list endpoint uses; the
    # user is loaded because the live caller holds only a `user_id`, and one
    # that no longer resolves is offered nothing.
    viewer = session.get(User, user_id)
    profiles: list[Profile] = []
    if viewer is not None:
        profiles = list(
            session.execute(
                select(Profile)
                .where(
                    Profile.is_active.is_(True),
                    Profile.id.in_(readable_profile_ids(viewer)),
                )
                .order_by(Profile.name)
            ).scalars()
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
