from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from call_monitor.db.models import (
    Call,
    CallRanking,
    DigestRun,
    Profile,
    Subscription,
    User,
    UserCallDismissal,
)

DIGEST_TOP_THRESHOLD = 60


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards (`%`, `_`) so admin-entered terms like '100%'
    match literally, not as wildcards. The backslash is also escaped in
    case a term contains one already."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
DIGEST_BORDERLINE_LOWER = 45
DIGEST_BUCKET_SIZE = 5


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


@dataclass
class DigestProfileSection:
    profile_id: int
    profile_name: str
    entries: list[DigestCallEntry]


@dataclass
class DigestPayload:
    user_id: int
    user_email: str
    sections: list[DigestProfileSection]
    total_calls: int


def assemble_for_user(
    session: Session, *, user_id: int, now: datetime
) -> DigestPayload | None:
    """Assemble a digest for the given user. Returns None if nothing to send.

    No DB writes — pure read. The caller (run_digest) writes the digest_runs
    row only AFTER a successful send."""
    user = session.get(User, user_id)
    if user is None or user.digest_disabled_at is not None:
        return None

    # 1. Subscribed active profiles, digest_enabled
    profiles = session.execute(
        select(Profile)
        .join(Subscription, Subscription.profile_id == Profile.id)
        .where(
            Subscription.user_id == user_id,
            Subscription.digest_enabled.is_(True),
            Profile.is_active.is_(True),
        )
        .order_by(Profile.id)  # tie-break ordering for dedup
    ).scalars().all()
    if not profiles:
        return None

    # 2. Exclusion sets
    dismissed_ids = set(
        session.execute(
            select(UserCallDismissal.call_id).where(UserCallDismissal.user_id == user_id)
        ).scalars().all()
    )
    prior_ids: set[int] = set()
    for run in session.execute(
        select(DigestRun.included_call_ids).where(DigestRun.user_id == user_id)
    ).scalars().all():
        prior_ids.update(run or [])

    excluded = dismissed_ids | prior_ids

    # 3. For each profile, build candidate entries (pre-dedup)
    # Track: dict[call_id, list[(profile, bucket, ranking_or_none)]]
    per_call: dict[int, list[tuple[Profile, str, CallRanking | None]]] = defaultdict(list)
    call_objs: dict[int, Call] = {}

    for profile in profiles:
        must_terms = [
            t.strip().lower()
            for t in (profile.structured.get("must_include") or [])
            if t and t.strip()
        ]

        # 3a. must-include candidates (any score, any ranking, even unranked)
        # Per spec line 417: match against title, summary, AND keywords.
        # We collapse keywords to a space-joined string and ilike-search;
        # this keeps the SQL simple and lets PG's text matching handle
        # case-insensitivity. The GIN index on keywords is bypassed (full
        # scan), but at v1 corpus size (thousands of calls) this is fine.
        if must_terms:
            keywords_str = func.array_to_string(Call.keywords, " ")
            must_q = (
                select(Call)
                .where(
                    Call.id.notin_(excluded) if excluded else True,
                    or_(Call.deadline.is_(None), Call.deadline > now),
                    or_(*[
                        Call.title.ilike(f"%{_escape_like(t)}%", escape="\\")
                        | Call.summary.ilike(f"%{_escape_like(t)}%", escape="\\")
                        | keywords_str.ilike(f"%{_escape_like(t)}%", escape="\\")
                        for t in must_terms
                    ]),
                )
            )
            for c in session.execute(must_q).scalars().all():
                # need this profile's ranking (if any) for score display
                rk = session.execute(
                    select(CallRanking).where(
                        CallRanking.call_id == c.id, CallRanking.profile_id == profile.id
                    )
                ).scalar_one_or_none()
                per_call[c.id].append((profile, "must_include", rk))
                call_objs[c.id] = c

        # 3b. top candidates: score >= threshold
        top_q = (
            select(Call, CallRanking)
            .join(CallRanking, CallRanking.call_id == Call.id)
            .where(
                CallRanking.profile_id == profile.id,
                CallRanking.aggregate_score >= DIGEST_TOP_THRESHOLD,
                Call.id.notin_(excluded) if excluded else True,
                or_(Call.deadline.is_(None), Call.deadline > now),
            )
            .order_by(CallRanking.aggregate_score.desc())
            .limit(50)  # over-fetch; dedup may drop some
        )
        for c, rk in session.execute(top_q).all():
            per_call[c.id].append((profile, "top", rk))
            call_objs[c.id] = c

        # 3c. borderline candidates: 45 <= score < threshold
        border_q = (
            select(Call, CallRanking)
            .join(CallRanking, CallRanking.call_id == Call.id)
            .where(
                CallRanking.profile_id == profile.id,
                CallRanking.aggregate_score >= DIGEST_BORDERLINE_LOWER,
                CallRanking.aggregate_score < DIGEST_TOP_THRESHOLD,
                Call.id.notin_(excluded) if excluded else True,
                or_(Call.deadline.is_(None), Call.deadline > now),
            )
            .order_by(CallRanking.aggregate_score.desc())
            .limit(50)
        )
        for c, rk in session.execute(border_q).all():
            per_call[c.id].append((profile, "borderline", rk))
            call_objs[c.id] = c

    # 4. Dedup: each call lands in exactly one (profile, bucket).
    # Selection rule: must_include bucket beats top beats borderline (by design —
    # must_include is the user's hard filter and overrides score-based ranking).
    # Within a bucket: highest score wins; tie-break by lowest profile_id (spec line 420).
    _BUCKET_PRIORITY = {"must_include": 0, "top": 1, "borderline": 2}
    primary_assignment: dict[int, tuple[Profile, str, CallRanking | None]] = {}
    secondary_profile_names: dict[int, list[str]] = defaultdict(list)
    for cid, candidates in per_call.items():
        # Pick "primary": min by (bucket priority, -score, profile_id)
        def _key(item):
            prof, bucket, rk = item
            score = rk.aggregate_score if rk else -1
            return (_BUCKET_PRIORITY[bucket], -score, prof.id)
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
        )
        grouped[profile.id][bucket].append(entry)

    # Sort and cap each profile's buckets
    sections: list[DigestProfileSection] = []
    profiles_by_id = {p.id: p for p in profiles}
    for pid in sorted(grouped, key=lambda p: profiles_by_id[p].name):
        buckets = grouped[pid]
        for bucket_name in ("top", "borderline"):
            buckets[bucket_name].sort(
                key=lambda e: e.score or 0, reverse=True
            )
            buckets[bucket_name] = buckets[bucket_name][:DIGEST_BUCKET_SIZE]
        # must_include kept fully (no cap) since they're the user's escape hatch
        ordered = (
            buckets["must_include"] + buckets["top"] + buckets["borderline"]
        )
        if not ordered:
            continue
        sections.append(
            DigestProfileSection(
                profile_id=pid,
                profile_name=profiles_by_id[pid].name,
                entries=ordered,
            )
        )

    if not sections:
        return None

    total = sum(len(sec.entries) for sec in sections)
    return DigestPayload(
        user_id=user_id,
        user_email=user.email,
        sections=sections,
        total_calls=total,
    )
