import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)


def _profile(s, name="Group A", kind=ProfileKind.ORGANIZATIONAL_UNIT):
    u = User(email="creator@umu.se")
    s.add(u)
    s.flush()
    p = Profile(name=name, kind=kind, description="", structured={}, created_by=u.id)
    s.add(p)
    s.flush()
    return p


def _call(s):
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(source_id=src.id, external_id="C-1", url="https://x", title="Test")
    s.add(c)
    s.flush()
    return c


def test_can_create_profile(pg_session_factory):
    with pg_session_factory() as s:
        p = _profile(s)
        s.commit()
        s.refresh(p)
        assert p.id is not None
        assert p.is_active is True
        assert p.created_at is not None
        assert p.kind == ProfileKind.ORGANIZATIONAL_UNIT


def test_call_ranking_composite_pk(pg_session_factory):
    with pg_session_factory() as s:
        p = _profile(s)
        c = _call(s)
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=p.id,
                aggregate_score=72,
                rationale="Good fit",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
                raw_response={"score": 72, "rationale": "Good fit"},
            )
        )
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        # Same (call_id, profile_id) → duplicate PK
        p = s.execute(select(Profile)).scalar_one()
        c = s.execute(select(Call).where(Call.external_id == "C-1")).scalar_one()
        s.add(
            CallRanking(
                call_id=c.id,
                profile_id=p.id,
                aggregate_score=10,
                rationale="",
                model="",
                prompt_version="v2.0",
            )
        )
        s.commit()


def test_ranking_job_partial_unique_blocks_duplicate_pending(pg_session_factory):
    with pg_session_factory() as s:
        p = _profile(s)
        c = _call(s)
        s.add(RankingJob(call_id=c.id, profile_id=p.id, status=RankingJobStatus.PENDING))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        p = s.execute(select(Profile)).scalar_one()
        c = s.execute(select(Call).where(Call.external_id == "C-1")).scalar_one()
        s.add(RankingJob(call_id=c.id, profile_id=p.id, status=RankingJobStatus.PENDING))
        s.commit()


def test_ranking_job_terminal_states_can_coexist_with_fresh_pending(pg_session_factory):
    """A done/failed job must not block a fresh pending job for the same pair —
    that's how 're-rank everything' works after a prompt change."""
    with pg_session_factory() as s:
        p = _profile(s)
        c = _call(s)
        s.add(
            RankingJob(
                call_id=c.id,
                profile_id=p.id,
                status=RankingJobStatus.DONE,
                attempts=1,
            )
        )
        s.add(RankingJob(call_id=c.id, profile_id=p.id, status=RankingJobStatus.PENDING))
        s.commit()
        n = s.execute(select(RankingJob)).scalars().all()
    assert len(n) == 2


def test_profile_soft_delete_via_is_active(pg_session_factory):
    with pg_session_factory() as s:
        p = _profile(s, name="Inactive group")
        p.is_active = False
        s.commit()
        active = s.execute(select(Profile).where(Profile.is_active.is_(True))).scalars().all()
        inactive = s.execute(select(Profile).where(Profile.is_active.is_(False))).scalars().all()
    assert active == []
    assert len(inactive) == 1
