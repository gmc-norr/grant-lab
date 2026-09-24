from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from grant_lab.db.models import Call, Profile, RankingJob, RankingJobStatus, Source
from grant_lab.ranking.backfill import backfill_profile


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


def test_backfill_enqueues_every_open_call_whatever_its_age(pg_session_factory, profile_factory):
    """Spec D4: the window is openness. Published 1 day or 400 days ago is the same.

    The CLOSED call is the 30-day one, and the assertion names which calls got
    jobs: closing the 400-day call instead, or counting only, passes the old
    90-day publication window too (BF-0 and BF-1 either way)."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        _seed_calls(s, ages_days=[1, 30, 400])
        s.execute(
            update(Call)
            .where(Call.external_id == "BF-1")
            .values(deadline=datetime.now(UTC) - timedelta(days=1))
        )  # closed
        s.commit()
    backfill_profile(session_factory=pg_session_factory, profile_id=profile_id)
    with pg_session_factory() as s:
        enqueued = s.execute(
            select(Call.external_id).join(RankingJob, RankingJob.call_id == Call.id)
        ).scalars()
        assert sorted(enqueued) == ["BF-0", "BF-2"]


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


def test_backfill_bypasses_dead_letter_suppression(pg_session_factory, profile_factory):
    """Spec D4: backfill runs only after a human act (a profile edit that changed
    its rank inputs, a new profile's bootstrap, or the admin "Re-rank
    everything" override), so a dead-lettered pair -- recent or old -- is
    enqueued again. Suppression belongs to the automatic paths only."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        # call 0: recent dead-letter; call 1: old dead-letter. Both are enqueued.
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
    assert len(pending) == 2
    assert {j.call_id for j in pending} == {recent_call.id, old_call.id}
