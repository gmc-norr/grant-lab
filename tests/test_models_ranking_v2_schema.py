from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import (
    Call,
    CallRanking,
    CallRankingCriterion,
    Profile,
    ProfileKind,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)


def _seed_pair(s, *, email="u@ki.se"):
    """Seed a (call, profile) pair and return their ids."""
    u = User(email=email)
    s.add(u)
    s.flush()
    p = Profile(
        name=f"P-{email}", kind=ProfileKind.GROUP, description="", structured={
            "areas": [], "budget_min": None, "budget_max": None,
            "trl_min": None, "trl_max": None,
            "must_include": [], "hard_exclusions": [],
        }, created_by=u.id,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{email}", url="https://x", title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id, p.id


def test_call_rankings_dual_scheme_coexist(pg_session_factory):
    """A (call, profile) pair can have both v1 and v2 rows simultaneously."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="v1 says 70",
            model="gemma4:e4b-it-q4_K_M", prompt_version="v1",
            ranked_at=datetime.now(UTC),
        ))
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v2",
            aggregate_score=82, rationale="v2 says 82",
            model="gemma4:e4b-it-q4_K_M", prompt_version="v2.0",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        rows = s.execute(
            select(CallRanking).where(CallRanking.call_id == cid)
        ).scalars().all()
    assert sorted(r.scheme for r in rows) == ["v1", "v2"]


def test_call_rankings_same_scheme_duplicate_rejected(pg_session_factory):
    """PK is (call_id, profile_id, scheme) — duplicate within same scheme is rejected."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v1",
            aggregate_score=70, rationale="r",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v1",
            aggregate_score=80, rationale="other",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.commit()


def test_call_rankings_aggregate_reason_and_setwise_offset_default(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRanking(
            call_id=cid, profile_id=pid, scheme="v1",
            aggregate_score=0, rationale="",
            model="m", prompt_version="v1", ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        r = s.execute(select(CallRanking)).scalar_one()
    assert r.aggregate_reason is None
    assert r.setwise_offset == 0


def test_ranking_jobs_dual_scheme_active_simultaneously(pg_session_factory):
    """Partial unique index allows v1 + v2 active for the same (call, profile)."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v1",
            status=RankingJobStatus.PENDING,
        ))
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v2",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()
    with pg_session_factory() as s:
        jobs = s.execute(
            select(RankingJob).where(RankingJob.call_id == cid)
        ).scalars().all()
    assert sorted(j.scheme for j in jobs) == ["v1", "v2"]


def test_ranking_jobs_same_scheme_active_duplicate_rejected(pg_session_factory):
    """Within the same scheme, only one active job per (call, profile)."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v1",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.add(RankingJob(
            call_id=cid, profile_id=pid, scheme="v1",
            status=RankingJobStatus.PENDING,
        ))
        s.commit()


def test_profile_ranking_scheme_default_v1(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="prof@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name="P", kind=ProfileKind.GROUP, description="", structured={},
            created_by=u.id,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
    assert p.ranking_scheme == "v1"
    assert p.budget_currency is None


def test_call_ranking_criteria_round_trip(pg_session_factory):
    """The new table accepts inserts with the documented PK shape."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRankingCriterion(
            call_id=cid, profile_id=pid, scheme="v2",
            criterion="topic_fit", rating="4",
            rating_expected=4.2,
            rationale="aligned",
            quoted_evidence="from the call text",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        row = s.execute(select(CallRankingCriterion)).scalar_one()
    assert row.criterion == "topic_fit"
    assert row.rating == "4"
    assert row.rating_expected == pytest.approx(4.2)


def test_call_ranking_criteria_pk_includes_criterion(pg_session_factory):
    """Same (call, profile, scheme) can have multiple criteria rows — one per criterion."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s)
        s.add(CallRankingCriterion(
            call_id=cid, profile_id=pid, scheme="v2",
            criterion="topic_fit", rating="3",
            ranked_at=datetime.now(UTC),
        ))
        s.add(CallRankingCriterion(
            call_id=cid, profile_id=pid, scheme="v2",
            criterion="eligibility", rating="pass",
            ranked_at=datetime.now(UTC),
        ))
        s.commit()
    with pg_session_factory() as s:
        rows = s.execute(select(CallRankingCriterion)).scalars().all()
    assert {r.criterion for r in rows} == {"topic_fit", "eligibility"}
