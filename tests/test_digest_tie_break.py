from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    Subscription,
    User,
)
from call_manager.digest.assemble import assemble_for_user

_FUTURE = datetime(2099, 12, 31, tzinfo=UTC)


def _seed_two_equal_score_calls(s, *, cos_a: float | None, cos_b: float | None):
    """Two calls, same aggregate_score=80, different cosines. Higher cosine
    must sort first in the digest 'top' bucket."""
    u = User(email="tie@ki.se", status="active")
    s.add(u)
    s.flush()
    p = Profile(
        name="TieP",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="d",
        structured={"areas": ["AI"]},
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    s.add(Subscription(user_id=u.id, profile_id=p.id, digest_enabled=True))
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    ids = {}
    for tag, cos in (("A", cos_a), ("B", cos_b)):
        c = Call(
            source_id=src.id,
            external_id=tag,
            url=f"https://x/{tag}",
            title=f"Call {tag}",
            summary="s",
            deadline=_FUTURE,
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.flush()
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=p.id,
                aggregate_score=80,
                aggregate_reason=None,
                rationale="r",
                model="m",
                prompt_version="v4.1",
                raw_response={},
                embed_similarity=cos,
                ranked_at=datetime.now(UTC),
            )
        )
        ids[tag] = c.id
    return u.id, ids


def test_higher_cosine_sorts_first_within_equal_score_bucket(pg_session_factory):
    with pg_session_factory() as s:
        uid, ids = _seed_two_equal_score_calls(s, cos_a=0.40, cos_b=0.90)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=datetime.now(UTC))
    entries = payload.sections[0].entries
    order = [e.call_id for e in entries]
    assert order.index(ids["B"]) < order.index(ids["A"])  # 0.90 before 0.40


def test_null_cosine_sorts_last_within_equal_score_bucket(pg_session_factory):
    with pg_session_factory() as s:
        uid, ids = _seed_two_equal_score_calls(s, cos_a=None, cos_b=0.10)
        s.commit()
    with pg_session_factory() as s:
        payload = assemble_for_user(s, user_id=uid, now=datetime.now(UTC))
    order = [e.call_id for e in payload.sections[0].entries]
    assert order.index(ids["B"]) < order.index(ids["A"])  # 0.10 before NULL
