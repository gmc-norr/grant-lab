from datetime import UTC, datetime

from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    Profile,
    ProfileKind,
    RankingJob,
    Source,
    User,
)
from call_monitor.ranking.queue import enqueue_for_pair


def _seed(s, *, profile_scheme="v1"):
    u = User(email=f"u-{profile_scheme}@ki.se")
    s.add(u)
    s.flush()
    p = Profile(
        name=f"P-{profile_scheme}", kind=ProfileKind.GROUP, description="",
        structured={}, created_by=u.id, ranking_scheme=profile_scheme,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{profile_scheme}", url="https://x", title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id, p.id


def test_enqueue_for_pair_scheme_v1_creates_v1_job(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s, profile_scheme="v1")
        s.commit()
    with pg_session_factory() as s:
        created = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v1")
        s.commit()
    assert created is True
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
    assert job.scheme == "v1"


def test_enqueue_for_pair_scheme_v2_creates_v2_job(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s, profile_scheme="v2")
        s.commit()
    with pg_session_factory() as s:
        created = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v2")
        s.commit()
    assert created is True
    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
    assert job.scheme == "v2"


def test_enqueue_for_pair_idempotent_within_same_scheme(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()
    with pg_session_factory() as s:
        first = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v1")
        second = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v1")
        s.commit()
    assert first is True
    assert second is False  # ON CONFLICT DO NOTHING
    with pg_session_factory() as s:
        rows = s.execute(select(RankingJob)).scalars().all()
    assert len(rows) == 1


def test_enqueue_for_pair_v1_and_v2_can_coexist_active(pg_session_factory):
    """Same (call, profile) can have both schemes active simultaneously —
    Plan C's compare mode depends on this."""
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        s.commit()
    with pg_session_factory() as s:
        a = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v1")
        b = enqueue_for_pair(s, call_id=cid, profile_id=pid, scheme="v2")
        s.commit()
    assert a is True and b is True
    with pg_session_factory() as s:
        schemes = sorted(j.scheme for j in s.execute(select(RankingJob)).scalars().all())
    assert schemes == ["v1", "v2"]
