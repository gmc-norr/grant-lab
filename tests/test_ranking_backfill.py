from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from call_manager.db.models import Call, Profile, RankingJob, RankingJobStatus, Source
from call_manager.ranking.backfill import BACKFILL_DAYS, backfill_profile


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


def test_backfill_skips_pair_with_recent_dead_lettered_job(pg_session_factory, profile_factory):
    """A terminally-FAILED (dead-lettered) pair must not be re-enqueued by
    backfill — otherwise every backfill run re-tries a pair that already
    burned all its attempts. An OLD failed job (past the window) is still
    eligible again."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        # call 0: recent dead-letter → suppressed; call 1: old dead-letter → eligible
        _seed_calls(s, ages_days=[1, 1])
        calls = s.execute(select(Call).order_by(Call.external_id)).scalars().all()
        recent_call, old_call = calls[0], calls[1]
        for c, age_days in ((recent_call, 1), (old_call, 30)):
            job = RankingJob(
                call_id=c.id,
                profile_id=profile_id,
                status=RankingJobStatus.FAILED,
                attempts=5,
                last_error="dead-lettered",
            )
            s.add(job)
            s.flush()
            s.execute(
                update(RankingJob)
                .where(RankingJob.id == job.id)
                .values(updated_at=datetime.now(UTC) - timedelta(days=age_days))
            )
        s.commit()

    backfill_profile(session_factory=pg_session_factory, profile_id=profile_id)

    with pg_session_factory() as s:
        pending = (
            s.execute(select(RankingJob).where(RankingJob.status == RankingJobStatus.PENDING))
            .scalars()
            .all()
        )
    # Only the old-dead-letter call gets re-enqueued.
    assert len(pending) == 1
    assert pending[0].call_id == old_call.id
