"""Golden-set CRUD + candidate mining. Pure query helpers over a passed-in
Session; the web route owns the transaction (commit/rollback)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, exists, or_, select

from call_manager.db.models import (
    Call,
    CallRanking,
    GoldenSetTriple,
    Profile,
    Subscription,
    UserCallDismissal,
)

_CANDIDATE_LIMIT = 20


@dataclass(frozen=True)
class TripleRow:
    id: int
    call_id: int
    profile_id: int
    expected_score: int
    note: str | None
    call_title: str
    profile_name: str


@dataclass(frozen=True)
class CandidateRow:
    call_id: int
    profile_id: int
    call_title: str
    profile_name: str
    aggregate_score: int
    embed_similarity: float | None
    reason: str


def add_triple(session, *, call_id, profile_id, expected_score, note=None, created_by=None) -> int:
    row = GoldenSetTriple(
        call_id=call_id,
        profile_id=profile_id,
        expected_score=expected_score,
        note=note,
        created_by=created_by,
    )
    session.add(row)
    session.flush()
    return row.id


def delete_triple(session, *, triple_id: int) -> None:
    row = session.get(GoldenSetTriple, triple_id)
    if row is not None:
        session.delete(row)


def list_triples(session) -> list[TripleRow]:
    rows = session.execute(
        select(GoldenSetTriple, Call.title, Profile.name)
        .join(Call, Call.id == GoldenSetTriple.call_id)
        .join(Profile, Profile.id == GoldenSetTriple.profile_id)
        .order_by(GoldenSetTriple.created_at.desc())
    ).all()
    return [
        TripleRow(
            id=t.id,
            call_id=t.call_id,
            profile_id=t.profile_id,
            expected_score=t.expected_score,
            note=t.note,
            call_title=title,
            profile_name=name,
        )
        for t, title, name in rows
    ]


def _not_already_a_triple():
    """Correlated NOT EXISTS against golden_set_triples for a CallRanking row."""
    return ~exists().where(
        and_(
            GoldenSetTriple.call_id == CallRanking.call_id,
            GoldenSetTriple.profile_id == CallRanking.profile_id,
        )
    )


def candidates_dismissed_high_score(session) -> list[CandidateRow]:
    """Ranked >= 60 for a profile, dismissed by a user subscribed to that profile."""
    q = (
        select(CallRanking, Call.title, Profile.name)
        .join(Call, Call.id == CallRanking.call_id)
        .join(Profile, Profile.id == CallRanking.profile_id)
        .where(
            CallRanking.aggregate_score >= 60,
            _not_already_a_triple(),
            exists().where(
                and_(
                    UserCallDismissal.call_id == CallRanking.call_id,
                    Subscription.user_id == UserCallDismissal.user_id,
                    Subscription.profile_id == CallRanking.profile_id,
                )
            ),
        )
        .order_by(CallRanking.aggregate_score.desc())
        .limit(_CANDIDATE_LIMIT)
    )
    return [
        CandidateRow(
            call_id=r.call_id,
            profile_id=r.profile_id,
            call_title=title,
            profile_name=name,
            aggregate_score=r.aggregate_score,
            embed_similarity=r.embed_similarity,
            reason="dismissed_high_score",
        )
        for r, title, name in session.execute(q).all()
    ]


def candidates_cosine_disagreement(session) -> list[CandidateRow]:
    """High cosine + low score, or low cosine + high score."""
    q = (
        select(CallRanking, Call.title, Profile.name)
        .join(Call, Call.id == CallRanking.call_id)
        .join(Profile, Profile.id == CallRanking.profile_id)
        .where(
            CallRanking.embed_similarity.is_not(None),
            or_(
                and_(CallRanking.embed_similarity >= 0.5, CallRanking.aggregate_score <= 20),
                and_(CallRanking.embed_similarity < 0.3, CallRanking.aggregate_score >= 60),
            ),
            _not_already_a_triple(),
        )
        .order_by(CallRanking.embed_similarity.desc())
        .limit(_CANDIDATE_LIMIT)
    )
    return [
        CandidateRow(
            call_id=r.call_id,
            profile_id=r.profile_id,
            call_title=title,
            profile_name=name,
            aggregate_score=r.aggregate_score,
            embed_similarity=r.embed_similarity,
            reason="cosine_disagreement",
        )
        for r, title, name in session.execute(q).all()
    ]


def candidates_escalation_disagreement(session) -> list[CandidateRow]:
    """escalated rows where Sonnet moved the score >= 40 points from Gemma's.

    Phase 2 stub: the escalated / escalated_from_score columns it needs do not
    exist until Phase 4 (Task 21). Returns [] here so this module imports and
    tests cleanly now; Task 26 replaces this body with the real query and the
    route wires it in. The session arg is accepted for signature stability.
    """
    return []
