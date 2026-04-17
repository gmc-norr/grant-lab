from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from call_monitor.db.models import Call, Profile, RankingJob, Source
from call_monitor.ranking.backfill import BACKFILL_DAYS, backfill_profile


def _seed_calls(s, ages_days):
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    now = datetime.now(UTC)
    for i, age in enumerate(ages_days):
        s.add(
            Call(
                source_id=src.id,
                external_id=f"BF-{i}",
                url=f"https://x/{i}",
                title=f"Call {i}",
                published_at=now - timedelta(days=age),
            )
        )
    s.commit()


def test_backfill_enqueues_jobs_for_recent_calls(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        _seed_calls(s, ages_days=[1, 30, 89, BACKFILL_DAYS + 5, 200])

    backfill_profile(session_factory=pg_session_factory, profile_id=profile_id)

    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
        # Three calls within the BACKFILL_DAYS window
    assert len(jobs) == 3


def test_backfill_skips_inactive_profile(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        s.get(Profile, profile_id).is_active = False
        s.commit()
        _seed_calls(s, ages_days=[1, 5])

    backfill_profile(session_factory=pg_session_factory, profile_id=profile_id)

    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert jobs == []


def test_backfill_is_idempotent(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        _seed_calls(s, ages_days=[1, 5])

    backfill_profile(session_factory=pg_session_factory, profile_id=profile_id)
    backfill_profile(session_factory=pg_session_factory, profile_id=profile_id)

    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(jobs) == 2  # not 4 — partial unique index dedups
