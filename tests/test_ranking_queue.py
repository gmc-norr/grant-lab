from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
)
from call_monitor.ranking.queue import (
    claim_pending_jobs,
    enqueue_for_pair,
    mark_done,
    mark_failed,
    reconcile_missing_rankings,
    requeue_stuck_running,
)


def _seed_call(s) -> int:
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(source_id=src.id, external_id="Q-1", url="https://x", title="T")
    s.add(c)
    s.flush()
    return c.id


def test_enqueue_creates_pending_job(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        was_new = enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        s.commit()
    assert was_new is True
    with pg_session_factory() as s:
        rows = s.execute(select(RankingJob)).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == RankingJobStatus.PENDING


def test_enqueue_is_idempotent_on_duplicate_active_pair(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        s.commit()
    with pg_session_factory() as s:
        was_new = enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        s.commit()
    assert was_new is False
    with pg_session_factory() as s:
        rows = s.execute(select(RankingJob)).scalars().all()
    assert len(rows) == 1


def test_claim_pending_marks_running_and_returns_jobs(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        s.commit()
    with pg_session_factory() as s:
        claimed = claim_pending_jobs(s, limit=10)
        s.commit()
    assert len(claimed) == 1
    assert claimed[0].status == RankingJobStatus.RUNNING


def test_claim_skips_already_running(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        s.commit()
    with pg_session_factory() as s:
        claim_pending_jobs(s, limit=10)
        s.commit()
    with pg_session_factory() as s:
        again = claim_pending_jobs(s, limit=10)
        s.commit()
    assert again == []


def test_mark_done_terminates_job(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        claimed = claim_pending_jobs(s, limit=1)
        mark_done(s, claimed[0].id)
        s.commit()
    with pg_session_factory() as s:
        row = s.execute(select(RankingJob)).scalar_one()
    assert row.status == RankingJobStatus.DONE


def test_mark_failed_records_error_and_increments_attempts(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        claimed = claim_pending_jobs(s, limit=1)
        mark_failed(s, claimed[0].id, error="Ollama 503", terminal=False)
        s.commit()
    with pg_session_factory() as s:
        row = s.execute(select(RankingJob)).scalar_one()
    assert row.status == RankingJobStatus.PENDING  # retry
    assert row.attempts == 1
    assert row.last_error == "Ollama 503"


def test_mark_failed_terminal_dead_letters(pg_session_factory, profile_factory):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        claimed = claim_pending_jobs(s, limit=1)
        mark_failed(s, claimed[0].id, error="parse error", terminal=True)
        s.commit()
    with pg_session_factory() as s:
        row = s.execute(select(RankingJob)).scalar_one()
    assert row.status == RankingJobStatus.FAILED


def test_requeue_stuck_running_recovers_crashed_workers(pg_session_factory, profile_factory):
    """If a worker crashed mid-tick the claimed jobs are stuck in RUNNING
    forever (claim_pending_jobs only finds PENDING). The reaper flips
    them back to PENDING so the next tick retries."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        claimed = claim_pending_jobs(s, limit=1)
        # Simulate "stuck for >5 minutes": rewind updated_at directly.
        s.execute(
            update(RankingJob)
            .where(RankingJob.id == claimed[0].id)
            .values(updated_at=datetime.now(UTC) - timedelta(minutes=10))
        )
        s.commit()

    with pg_session_factory() as s:
        n = requeue_stuck_running(s)
        s.commit()
    assert n == 1

    with pg_session_factory() as s:
        row = s.execute(select(RankingJob)).scalar_one()
    assert row.status == RankingJobStatus.PENDING
    # attempts NOT incremented — the previous run never reached mark_failed
    assert row.attempts == 0


def _seed_call_with(s, *, external_id: str, published: datetime | None) -> int:
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=external_id, url=f"https://x/{external_id}",
        title=f"T-{external_id}", published_at=published,
    )
    s.add(c)
    s.flush()
    return c.id


def test_reconcile_enqueues_missing_pair(pg_session_factory, profile_factory):
    """Closes the orchestrator gap: a recent call with an active profile
    but no CallRanking and no active RankingJob — reconcile must enqueue."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call_with(
            s, external_id="RC-1", published=datetime.now(UTC) - timedelta(days=1)
        )
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s)
        s.commit()

    assert n == 1
    with pg_session_factory() as s:
        jobs = s.execute(
            select(RankingJob).where(RankingJob.call_id == call_id,
                                     RankingJob.profile_id == profile_id)
        ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].status == RankingJobStatus.PENDING


def test_reconcile_skips_already_ranked_pair(pg_session_factory, profile_factory):
    """If a CallRanking already exists the pair is done — no enqueue."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call_with(
            s, external_id="RC-2", published=datetime.now(UTC) - timedelta(days=2)
        )
        s.add(CallRanking(
            call_id=call_id, profile_id=profile_id, aggregate_score=50,
            rationale="", model="m", prompt_version="v3.0", raw_response={},
        ))
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s)
        s.commit()

    assert n == 0
    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert jobs == []


def test_reconcile_skips_pair_with_active_job(pg_session_factory, profile_factory):
    """If a pending/running job already covers the pair, the ON CONFLICT
    guard on the partial unique index skips the insert — no duplicate."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call_with(
            s, external_id="RC-3", published=datetime.now(UTC) - timedelta(days=3)
        )
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s)
        s.commit()

    assert n == 0
    with pg_session_factory() as s:
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(jobs) == 1  # original still there, no duplicate


def test_reconcile_ignores_inactive_profile(pg_session_factory, profile_factory):
    """Inactive profiles don't rank — reconcile must not enqueue for them."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        s.get(Profile, profile_id).is_active = False
        s.commit()
    with pg_session_factory() as s:
        _seed_call_with(
            s, external_id="RC-4", published=datetime.now(UTC) - timedelta(days=1)
        )
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s)
        s.commit()

    assert n == 0


def test_reconcile_ignores_old_calls(pg_session_factory, profile_factory):
    """Older-than-cutoff calls are out of scope — a profile that created
    yesterday shouldn't trigger a re-ranking of a 2-year-old call."""
    profile_factory()
    with pg_session_factory() as s:
        _seed_call_with(
            s, external_id="RC-5",
            published=datetime.now(UTC) - timedelta(days=365),
        )
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s)
        s.commit()

    assert n == 0


def test_reconcile_ignores_calls_with_null_published_at(
    pg_session_factory, profile_factory
):
    """We key the cutoff off published_at; a NULL means we can't bound
    its age, so we leave it to the normal backfill-on-demand path (same
    behavior as backfill_profile)."""
    profile_factory()
    with pg_session_factory() as s:
        _seed_call_with(s, external_id="RC-6", published=None)
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s)
        s.commit()

    assert n == 0


def test_requeue_stuck_running_leaves_recently_running_alone(pg_session_factory, profile_factory):
    """A job that's actively running (updated within the threshold) must
    NOT be requeued — that would risk double-running the same job."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        claim_pending_jobs(s, limit=1)
        s.commit()

    with pg_session_factory() as s:
        n = requeue_stuck_running(s)
        s.commit()
    assert n == 0

    with pg_session_factory() as s:
        row = s.execute(select(RankingJob)).scalar_one()
    assert row.status == RankingJobStatus.RUNNING
