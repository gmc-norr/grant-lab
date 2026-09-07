"""Golden-set CRUD + candidate mining. Pure query helpers over a passed-in
Session; the web route owns the transaction (commit/rollback)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import and_, exists, func, or_, select, tuple_

from grant_lab.db.models import (
    Call,
    CallDecision,
    CallRanking,
    GoldenSetTriple,
    Profile,
)

_CANDIDATE_LIMIT = 20
_NOTE_CAP = 3


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
    # The HUMANS' own words, not the mining reason above. Unattributed (D3),
    # hidden decisions only, newest decision first, at most _NOTE_CAP.
    # Empty for the two candidate queries that need no decision at all.
    user_notes: list[str] = field(default_factory=list)


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


def _notes_for(session, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], list[str]]:
    """The hidden decisions' notes for each (call_id, profile_id) pair.

    ONE query for the whole candidate page, not one per candidate.

    `state == "hidden"` is applied HERE, in the WHERE clause, and the cap is
    applied to the filtered rows -- so the filter precedes the cap (D7). A
    saved decision can carry a note too, and letting one through would file
    "we should apply for this" as evidence that the model scored it too high.

    Ordered newest DECISION first. There is no note-edit timestamp and this
    feature does not add one, so a note edited today on a decision made in
    March sorts as March. `id` breaks ties, which two decisions committed in
    the same transaction will have.
    """
    if not pairs:
        return {}
    # The cap is applied PER PAIR, in SQL, by a window -- not by a page-wide
    # LIMIT over a global ORDER BY.
    #
    # A page-wide `.limit(_CANDIDATE_LIMIT * _NOTE_CAP * 10)` was the obvious
    # way to bound this read, and it is silently wrong. That budget is spent in
    # `decided_at DESC` order across ALL pairs, which has nothing to do with
    # the page's own `aggregate_score DESC` ordering -- so once the page's
    # annotated hidden decisions exceed the budget, the candidate whose hides
    # are OLDEST returns [] with no signal. Measured 2026-09-07: 32 readers
    # each annotating 19 of the 20 candidates is 611 rows, and the top-scoring
    # candidate (99, position #1, hidden 200 days earlier) lost every note it
    # had. That is the candidate whose reason a labeller can least reconstruct.
    #
    # The window is exactly bounded at _CANDIDATE_LIMIT * _NOTE_CAP rows and
    # cannot starve one pair to feed another.
    rn = (
        func.row_number()
        .over(
            partition_by=(CallDecision.call_id, CallDecision.profile_id),
            order_by=(CallDecision.decided_at.desc(), CallDecision.id.desc()),
        )
        .label("rn")
    )
    ranked = (
        select(CallDecision.call_id, CallDecision.profile_id, CallDecision.note, rn)
        .where(
            tuple_(CallDecision.call_id, CallDecision.profile_id).in_(pairs),
            CallDecision.state == "hidden",
            CallDecision.note.is_not(None),
        )
        .subquery()
    )
    out: dict[tuple[int, int], list[str]] = {}
    rows = session.execute(
        select(ranked.c.call_id, ranked.c.profile_id, ranked.c.note)
        .where(ranked.c.rn <= _NOTE_CAP)
        .order_by(ranked.c.call_id, ranked.c.profile_id, ranked.c.rn)
    ).all()
    for call_id, profile_id, note in rows:
        out.setdefault((call_id, profile_id), []).append(note)
    return out


def candidates_dismissed_high_score(session) -> list[CandidateRow]:
    """Ranked >= 60 for a profile, and HIDDEN by someone on THAT SAME profile.

    The decision now carries its own profile_id, so this is a direct correlation
    rather than a join through `subscriptions` to guess which profile a global
    dismissal belonged to.

    This DELIBERATELY WIDENS who can contribute a negative label, from
    subscribers of the profile to READERS of it (D13). Task 1's write rule
    already requires the hider to be able to read the profile, and a hide on
    profile P is a statement about P — which is what "a decision is per profile"
    means. Pinned by
    tests/test_golden_set_mining.py::test_candidates_dismissed_high_score_sees_a_non_subscribed_readers_hide.
    """
    q = (
        select(CallRanking, Call.title, Profile.name)
        .join(Call, Call.id == CallRanking.call_id)
        .join(Profile, Profile.id == CallRanking.profile_id)
        .where(
            CallRanking.aggregate_score >= 60,
            _not_already_a_triple(),
            exists().where(
                and_(
                    CallDecision.call_id == CallRanking.call_id,
                    CallDecision.profile_id == CallRanking.profile_id,
                    # A SAVED call is not a negative label. Without this the
                    # golden set mines the calls the user explicitly KEPT as
                    # "the model scored this too high" evidence.
                    CallDecision.state == "hidden",
                )
            ),
        )
        .order_by(CallRanking.aggregate_score.desc())
        .limit(_CANDIDATE_LIMIT)
    )
    found = session.execute(q).all()
    notes = _notes_for(session, [(r.call_id, r.profile_id) for r, _t, _n in found])
    return [
        CandidateRow(
            call_id=r.call_id,
            profile_id=r.profile_id,
            call_title=title,
            profile_name=name,
            aggregate_score=r.aggregate_score,
            embed_similarity=r.embed_similarity,
            reason="dismissed_high_score",
            user_notes=notes.get((r.call_id, r.profile_id), []),
        )
        for r, title, name in found
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
    """escalated rows where Sonnet moved the score >= 40 points from Gemma's."""
    q = (
        select(CallRanking, Call.title, Profile.name)
        .join(Call, Call.id == CallRanking.call_id)
        .join(Profile, Profile.id == CallRanking.profile_id)
        .where(
            CallRanking.escalated.is_(True),
            CallRanking.escalated_from_score.is_not(None),
            func.abs(CallRanking.escalated_from_score - CallRanking.aggregate_score) >= 40,
            _not_already_a_triple(),
        )
        .order_by(func.abs(CallRanking.escalated_from_score - CallRanking.aggregate_score).desc())
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
            reason="escalation_disagreement",
        )
        for r, title, name in session.execute(q).all()
    ]
