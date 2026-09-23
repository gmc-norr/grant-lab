from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from grant_lab.db.models import (
    Call,
    CallDecision,
    CallRanking,
    DigestRun,
    Profile,
    Subscription,
    User,
)
from grant_lab.ranking.must_include import (
    normalize_must_terms,
    pg_word_boundary_pattern,
)
from grant_lab.web.profile_access import readable_profile_ids

DIGEST_TOP_THRESHOLD = 60


# The borderline floor must sit ON a reachable score, not between two of them.
# `aggregate_score` is `topic_fit * 20` over an INTEGER rating, so the only
# values a ranking can take are 0/20/40/50/60/80/100 (50 is the
# eligibility=unclear cap). Nothing lies between 40 and 45, so the former
# floor of 45 silently meant "50 or better" and a topic_fit of 2 fell into the
# same invisible pile as a 0 — it appeared in NO bucket and the digest treated
# it as nothing to send.
#
# Lowered 45 -> 40 on 2026-09-21. Measured before the change: of 679 ranked
# pairs, 30 scored 50 (the entire old band) and 4 scored exactly 40. One of
# those 4 was EU call 493, "Microbiome for early cancer prediction", which the
# remote model had rated 2/5 WITH a written rationale — a judgement worth
# showing as a maybe, which the floor threw away.
#
# This deliberately NO LONGER mirrors RANKING_ESCALATION_MIN_SCORE (45). The
# two numbers answer different questions: escalation asks "is this worth paying
# the remote model for?", this band asks "is this worth a human's eyes?". A
# score of 40 is frequently the remote model's OWN verdict after it demoted a
# local 60, so escalating 40s would only re-demote them out of the band again.
DIGEST_BORDERLINE_LOWER = 40
DIGEST_BUCKET_SIZE = 5
# must_include is the user's escape hatch, so it gets a larger cap than the
# score buckets — but it MUST be bounded. A broad term ("cancer", "research")
# ILIKE-matches title/summary/full_text/keywords across the whole non-excluded
# Call table; uncapped, the first digest after a backfill could put thousands
# of calls into one email and one DigestRun.included_call_ids. Soonest-deadline
# calls are kept first; the rest are summarized as "+N more" by the renderer.
DIGEST_MUST_INCLUDE_CAP = 25
# Whole-payload cap across all profile sections: prevents M profiles × 25 entries
# from flooding a multi-profile user's digest. Must-include entries are trimmed
# in section order (already sorted by profile name) keeping the first
# DIGEST_TOTAL_MUST_INCLUDE_CAP entries total (soonest-deadline first).
DIGEST_TOTAL_MUST_INCLUDE_CAP = 50


@dataclass
class DigestCallEntry:
    call_id: int
    title: str
    summary: str | None
    deadline: datetime | None
    score: int | None
    rationale: str | None
    url: str
    bucket: str  # "must_include" | "top" | "borderline"
    also_in_profiles: list[str] = field(default_factory=list)
    embed_similarity: float | None = None


@dataclass
class DigestProfileSection:
    profile_id: int
    profile_name: str
    entries: list[DigestCallEntry]
    more_count: int = 0


@dataclass
class DigestPayload:
    user_id: int
    user_email: str
    sections: list[DigestProfileSection]
    total_calls: int


def assemble_for_user(session: Session, *, user_id: int, now: datetime) -> DigestPayload | None:
    """Assemble a digest for the given user. Returns None if nothing to send.

    No DB writes — pure read. The caller (run_digest) writes the digest_runs
    row only AFTER a successful send."""
    user = session.get(User, user_id)
    if user is None or user.status != "active" or user.digest_disabled_at is not None:
        return None

    # 1. Subscribed active profiles, digest_enabled, and STILL READABLE.
    # The weekly email is the surface field-hiding cannot close: it carries a
    # profile's ranked calls, which reveal its content whether or not the API
    # shows its description. So the read rule is applied HERE, as a FILTER —
    # revocation must not touch `subscriptions`, or the user's digest_enabled
    # preference is destroyed and a re-share cannot bring the section back.
    profiles = (
        session.execute(
            select(Profile)
            .join(Subscription, Subscription.profile_id == Profile.id)
            .where(
                Subscription.user_id == user_id,
                Subscription.digest_enabled.is_(True),
                Profile.is_active.is_(True),
                Profile.id.in_(readable_profile_ids(user)),
            )
            .order_by(Profile.id)  # tie-break ordering for dedup
        )
        .scalars()
        .all()
    )
    if not profiles:
        return None

    # 2. Exclusion sets
    #
    # ONE SET PER PROFILE, not one per user: a decision is per profile now, so a
    # call hidden under P1 must still be able to reach the reader under P2.
    #
    # state == 'hidden' ONLY. Excluding every decision would drop SAVED calls out
    # of the digest -- the one place the user has explicitly said they want the
    # call. The spec's wording is exact: "a hidden call never appears in a later
    # digest for that profile."
    dismissed_ids: dict[int, set[int]] = defaultdict(set)
    for pid, cid in session.execute(
        select(CallDecision.profile_id, CallDecision.call_id).where(
            CallDecision.user_id == user_id,
            CallDecision.state == "hidden",
        )
    ).all():
        dismissed_ids[pid].add(cid)
    # Bound prior_ids to the last 78 weeks (~18 months). This MUST comfortably
    # exceed the longest realistic call deadline: EU portal calls routinely
    # carry 9-12 month deadlines, and a call still inside its deadline window
    # must stay in prior_ids or it gets RE-SENT as "new". A 26-week bound
    # dropped a 9-12 month-deadline call out of prior_ids around week 27 while
    # `Call.deadline > now` still kept it eligible — so it re-surfaced. 78
    # weeks covers the longest realistic deadline with margin. Memory cost is
    # trivial (~25 calls/week × 78 ≈ 2k ids), and the deadline filter still
    # prunes anything genuinely expired.
    prior_cutoff = now - timedelta(weeks=78)
    prior_ids: set[int] = set()
    for run in (
        session.execute(
            select(DigestRun.included_call_ids).where(
                DigestRun.user_id == user_id,
                DigestRun.sent_at >= prior_cutoff,
            )
        )
        .scalars()
        .all()
    ):
        prior_ids.update(run or [])

    # Rolling (NULL-deadline) calls never expire, so the 78-week window above
    # can't bound them: once a previously-sent rolling call ages out of prior_ids
    # it stays eligible (the `Call.deadline.is_(None)` branch in every candidate
    # query) and re-surfaces as "new". Exclude EVERY previously-sent call that
    # currently has a NULL deadline, with NO age bound. Deadline-bearing calls
    # remain governed by the 78-week window (sized to outlast the longest real
    # deadline); this only adds the unbounded rolling-call ids.
    all_prior_ids: set[int] = set()
    for run in (
        session.execute(select(DigestRun.included_call_ids).where(DigestRun.user_id == user_id))
        .scalars()
        .all()
    ):
        all_prior_ids.update(run or [])
    if all_prior_ids:
        prior_ids.update(
            session.execute(
                select(Call.id).where(Call.id.in_(all_prior_ids), Call.deadline.is_(None))
            )
            .scalars()
            .all()
        )

    # prior_ids is user-wide (a digest is one email); only the hidden half is
    # per profile, so the union is built inside the loop below.

    # 3. For each profile, build candidate entries (pre-dedup)
    # Track: dict[call_id, list[(profile, bucket, ranking_or_none)]]
    per_call: dict[int, list[tuple[Profile, str, CallRanking | None]]] = defaultdict(list)
    call_objs: dict[int, Call] = {}
    top_total_by_profile: dict[int, int] = {}

    for profile in profiles:
        # Materialize THIS profile's excluded id set once, so it isn't
        # re-iterated by each of the three candidate queries below (a Python set
        # re-iterates in arbitrary order for each in_ call).
        excluded_list = list(dismissed_ids.get(profile.id, frozenset()) | prior_ids)
        excl_clause = Call.id.notin_(excluded_list) if excluded_list else True

        # Shared normalization (min length) with the ranking path so the SAME
        # terms are considered on both sides (B11 Defect 1).
        must_terms = normalize_must_terms(profile.structured.get("must_include"))

        # 3a. must-include candidates (any score, any ranking, even unranked)
        # Per spec line 417: match against title, summary, full_text, AND
        # keywords. We collapse keywords to a space-joined string and match
        # each field with Postgres' case-insensitive POSIX regex operator
        # (~*) anchored at WORD boundaries (\m...\M) instead of a bare-substring
        # ILIKE. This keeps the digest path SEMANTICALLY IDENTICAL to the
        # ranking path's Python \b word-boundary match (B11 Defect 1) — a short
        # term like 'ai' no longer drags in calls that merely contain
        # 'training'/'brain'. The pg_trgm GIN indexes (migration 0033) still
        # accelerate ~* the same way they accelerated leading-wildcard ILIKE;
        # array_to_string(keywords) still scans but is cheap relative to
        # full_text. Sub-3-char terms are already dropped by normalize_must_terms.
        #
        # Codex Plan F regression fix: also match `Call.full_text`. After Plan F
        # moved Vinnova's substantive prose out of `summary` and into
        # `full_text`, a must-include term that lives only in the body text
        # would otherwise be silently missed by the digest bucket.
        if must_terms:
            keywords_str = func.array_to_string(Call.keywords, " ")
            # OUTER JOIN against this profile's ranking — must-include calls
            # are surfaced even if unranked (CallRanking will be None). This
            # mirrors the top/borderline pattern but with outerjoin to avoid
            # an N+1 follow-up query per matched call.
            must_q = (
                select(Call, CallRanking)
                .outerjoin(
                    CallRanking,
                    (CallRanking.call_id == Call.id) & (CallRanking.profile_id == profile.id),
                )
                .where(
                    excl_clause,
                    or_(Call.deadline.is_(None), Call.deadline > now),
                    or_(
                        *[
                            Call.title.op("~*")(pat)
                            | Call.summary.op("~*")(pat)
                            | Call.full_text.op("~*")(pat)
                            | keywords_str.op("~*")(pat)
                            for pat in (pg_word_boundary_pattern(t) for t in must_terms)
                        ]
                    ),
                )
                # Bound the escape hatch: soonest-deadline calls first (NULL
                # deadlines last), capped. Without this a broad term scans and
                # returns the entire non-excluded Call table. Over-fetch by one
                # bucket-worth so the renderer can show an accurate "+N more".
                .order_by(Call.deadline.asc().nullslast())
                .limit(DIGEST_MUST_INCLUDE_CAP)
            )
            for c, rk in session.execute(must_q).all():
                per_call[c.id].append((profile, "must_include", rk))
                call_objs[c.id] = c

        # 3b. top candidates: aggregate_score >= threshold
        top_conditions = [
            CallRanking.profile_id == profile.id,
            CallRanking.aggregate_score >= DIGEST_TOP_THRESHOLD,
            excl_clause,
            or_(Call.deadline.is_(None), Call.deadline > now),
        ]
        top_q = (
            select(Call, CallRanking)
            .join(CallRanking, CallRanking.call_id == Call.id)
            .where(*top_conditions)
            .order_by(
                CallRanking.aggregate_score.desc(),
                CallRanking.embed_similarity.desc().nullslast(),
            )
            .limit(50)  # over-fetch; dedup may drop some
        )
        for c, rk in session.execute(top_q).all():
            per_call[c.id].append((profile, "top", rk))
            call_objs[c.id] = c
        # Total strong matches for this profile == the dashboard's ?min_score=60
        # set; used for an honest "+N more in your dashboard" breadcrumb.
        top_total_by_profile[profile.id] = (
            session.scalar(
                select(func.count())
                .select_from(Call)
                .join(CallRanking, CallRanking.call_id == Call.id)
                .where(*top_conditions)
            )
            or 0
        )

        # 3c. borderline candidates: 45 <= aggregate_score < threshold
        border_q = (
            select(Call, CallRanking)
            .join(CallRanking, CallRanking.call_id == Call.id)
            .where(
                CallRanking.profile_id == profile.id,
                CallRanking.aggregate_score >= DIGEST_BORDERLINE_LOWER,
                CallRanking.aggregate_score < DIGEST_TOP_THRESHOLD,
                excl_clause,
                or_(Call.deadline.is_(None), Call.deadline > now),
            )
            .order_by(
                CallRanking.aggregate_score.desc(),
                CallRanking.embed_similarity.desc().nullslast(),
            )
            .limit(50)
        )
        for c, rk in session.execute(border_q).all():
            per_call[c.id].append((profile, "borderline", rk))
            call_objs[c.id] = c

    # 4. Dedup: each call lands in exactly one (profile, bucket).
    # Selection rule: must_include bucket beats top beats borderline (by design —
    # must_include is the user's hard filter and overrides score-based ranking).
    # Within a bucket: highest score wins; ties broken by highest cosine similarity; then by
    # lowest profile_id (spec line 420).
    _BUCKET_PRIORITY = {"must_include": 0, "top": 1, "borderline": 2}
    primary_assignment: dict[int, tuple[Profile, str, CallRanking | None]] = {}
    secondary_profile_names: dict[int, list[str]] = defaultdict(list)
    for cid, candidates in per_call.items():
        # Pick "primary": min by (bucket priority, -score, -cosine, profile_id).
        def _key(item):
            prof, bucket, rk = item
            score = rk.aggregate_score if rk else -1
            cos = rk.embed_similarity if (rk and rk.embed_similarity is not None) else -1.0
            return (_BUCKET_PRIORITY[bucket], -score, -cos, prof.id)

        candidates_sorted = sorted(candidates, key=_key)
        primary = candidates_sorted[0]
        primary_assignment[cid] = primary
        primary_profile_id = primary[0].id
        for prof, _bucket, _rk in candidates_sorted[1:]:
            if prof.id != primary_profile_id and prof.name not in secondary_profile_names[cid]:
                secondary_profile_names[cid].append(prof.name)

    # 5. Group by primary profile, cap top/borderline buckets at 5 each
    grouped: dict[int, dict[str, list[DigestCallEntry]]] = defaultdict(
        lambda: {"must_include": [], "top": [], "borderline": []}
    )
    for cid, (profile, bucket, rk) in primary_assignment.items():
        c = call_objs[cid]
        entry = DigestCallEntry(
            call_id=cid,
            title=c.title,
            summary=c.summary,
            deadline=c.deadline,
            score=rk.aggregate_score if rk else None,
            rationale=rk.rationale if rk else None,
            url=c.url,
            bucket=bucket,
            also_in_profiles=secondary_profile_names[cid],
            embed_similarity=rk.embed_similarity if rk else None,
        )
        grouped[profile.id][bucket].append(entry)

    # Sort and cap each profile's buckets
    sections: list[DigestProfileSection] = []
    profiles_by_id = {p.id: p for p in profiles}
    for pid in sorted(grouped, key=lambda p: profiles_by_id[p].name):
        buckets = grouped[pid]
        for bucket_name in ("top", "borderline"):
            buckets[bucket_name].sort(
                key=lambda e: (
                    e.score or 0,
                    e.embed_similarity if e.embed_similarity is not None else -1.0,
                ),
                reverse=True,
            )
            buckets[bucket_name] = buckets[bucket_name][:DIGEST_BUCKET_SIZE]
        # must_include is the user's escape hatch so it gets a larger cap than
        # the score buckets, but it is still bounded (see DIGEST_MUST_INCLUDE_CAP)
        # so a broad term can't flood the email. The per-profile dedup above may
        # leave fewer than the query LIMIT; cap again here defensively.
        buckets["must_include"].sort(key=lambda e: (e.deadline is None, e.deadline))
        buckets["must_include"] = buckets["must_include"][:DIGEST_MUST_INCLUDE_CAP]
        ordered = buckets["must_include"] + buckets["top"] + buckets["borderline"]
        if not ordered:
            continue
        more_count = max(0, top_total_by_profile.get(pid, 0) - len(buckets["top"]))
        sections.append(
            DigestProfileSection(
                profile_id=pid,
                profile_name=profiles_by_id[pid].name,
                entries=ordered,
                more_count=more_count,
            )
        )

    # Whole-payload cap: trim must_include entries across all sections
    # collectively so M profiles don't multiply the per-profile cap.
    _must_total = sum(
        sum(1 for e in sec.entries if e.bucket == "must_include") for sec in sections
    )
    if _must_total > DIGEST_TOTAL_MUST_INCLUDE_CAP:
        _remaining = DIGEST_TOTAL_MUST_INCLUDE_CAP
        for sec in sections:
            _mi = [e for e in sec.entries if e.bucket == "must_include"]
            _other = [e for e in sec.entries if e.bucket != "must_include"]
            _kept = _mi[:_remaining]
            _remaining = max(0, _remaining - len(_mi))
            sec.entries = _kept + _other

    if not sections:
        return None

    total = sum(len(sec.entries) for sec in sections)
    return DigestPayload(
        user_id=user_id,
        user_email=user.email,
        sections=sections,
        total_calls=total,
    )
