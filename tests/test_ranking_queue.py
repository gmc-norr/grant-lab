from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from grant_lab.db.models import (
    Call,
    CallRanking,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
)
from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
from grant_lab.ranking.queue import (
    _STUCK_AFTER,
    claim_pending_jobs,
    enqueue_for_pair,
    mark_done,
    mark_failed,
    reconcile_missing_rankings,
    requeue_stuck_running,
)
from tests.freshness_helpers import ctx_for

# Rewind comfortably past the stuck threshold so these tests stay correct if
# _STUCK_AFTER is retuned.
_PAST_STUCK = _STUCK_AFTER + timedelta(minutes=1)
CTX = ctx_for()


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
        # Simulate "stuck past the threshold": rewind updated_at directly.
        s.execute(
            update(RankingJob)
            .where(RankingJob.id == claimed[0].id)
            .values(updated_at=datetime.now(UTC) - _PAST_STUCK)
        )
        s.commit()

    with pg_session_factory() as s:
        n = requeue_stuck_running(s)
        s.commit()
    assert n == 1

    with pg_session_factory() as s:
        row = s.execute(select(RankingJob)).scalar_one()
    assert row.status == RankingJobStatus.PENDING
    # attempts IS now incremented on requeue so a job that reliably crashes
    # the worker eventually dead-letters instead of looping forever.
    assert row.attempts == 1


def test_requeue_stuck_running_dead_letters_persistent_crasher(pg_session_factory, profile_factory):
    """A job that has already consumed its attempts and is found stuck in
    RUNNING again must transition to FAILED (dead-letter), not back to
    PENDING — otherwise a payload that reliably crashes the worker before
    mark_failed runs would loop forever. Regression for the crash-loop gap."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call(s)
        enqueue_for_pair(s, call_id=call_id, profile_id=profile_id)
        claimed = claim_pending_jobs(s, limit=1)
        # Already at the last attempt (attempts == MAX_ATTEMPTS - 1 == 4),
        # and stuck in RUNNING past the threshold.
        s.execute(
            update(RankingJob)
            .where(RankingJob.id == claimed[0].id)
            .values(attempts=4, updated_at=datetime.now(UTC) - _PAST_STUCK)
        )
        s.commit()

    with pg_session_factory() as s:
        n = requeue_stuck_running(s)
        s.commit()
    assert n == 1

    with pg_session_factory() as s:
        row = s.execute(select(RankingJob)).scalar_one()
    assert row.status == RankingJobStatus.FAILED
    assert row.attempts == 5
    assert "crash loop" in (row.last_error or "")


def _seed_call_with(s, *, external_id: str, published: datetime | None) -> int:
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id,
        external_id=external_id,
        url=f"https://x/{external_id}",
        title=f"T-{external_id}",
        published_at=published,
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
        n = reconcile_missing_rankings(s, ctx=CTX)
        s.commit()

    assert n == 1
    with pg_session_factory() as s:
        jobs = (
            s.execute(
                select(RankingJob).where(
                    RankingJob.call_id == call_id, RankingJob.profile_id == profile_id
                )
            )
            .scalars()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0].status == RankingJobStatus.PENDING


def test_reconcile_skips_already_ranked_pair(pg_session_factory, profile_factory):
    """If a CURRENT CallRanking exists the pair is done — no enqueue.

    Migration 0075 changed what "done" means: reconcile now asks whether the
    ranking describes the profile's present content, not merely whether a row
    exists. A NULL `profile_fingerprint` after 0075 means a writer bug and is
    deliberately treated as stale, so this seed stamps the fingerprint the
    worker would have written.
    """
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call_with(
            s, external_id="RC-2", published=datetime.now(UTC) - timedelta(days=2)
        )
        s.add(
            CallRanking(
                call_id=call_id,
                profile_id=profile_id,
                aggregate_score=50,
                rationale="",
                model="m",
                prompt_version="v3.0",
                raw_response={},
                profile_fingerprint=rank_fingerprint(s.get(Profile, profile_id)),
                call_fingerprint=call_rank_fingerprint(s.get(Call, call_id)),
                scoring_fingerprint=CTX.scoring.fingerprint(),
            )
        )
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s, ctx=CTX)
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
        n = reconcile_missing_rankings(s, ctx=CTX)
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
        _seed_call_with(s, external_id="RC-4", published=datetime.now(UTC) - timedelta(days=1))
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s, ctx=CTX)
        s.commit()

    assert n == 0


def test_reconcile_includes_an_open_call_published_long_ago(pg_session_factory, profile_factory):
    """The window is openness, not publication date (spec F3): a call published
    a year ago with no deadline is still open, so its missing ranking is owed."""
    profile_factory()
    with pg_session_factory() as s:
        _seed_call_with(
            s,
            external_id="RC-5",
            published=datetime.now(UTC) - timedelta(days=365),
        )
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s, ctx=CTX)
        s.commit()

    assert n == 1


def test_reconcile_includes_a_call_with_null_published_at(pg_session_factory, profile_factory):
    """The window is openness, not publication date (spec F3): a NULL
    published_at says nothing about whether the call is open, so it is in."""
    profile_factory()
    with pg_session_factory() as s:
        _seed_call_with(s, external_id="RC-6", published=None)
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s, ctx=CTX)
        s.commit()

    assert n == 1


def test_reconcile_ignores_a_closed_call(pg_session_factory, profile_factory):
    """G7 through the wrapper."""
    profile_factory()
    with pg_session_factory() as s:
        cid = _seed_call_with(s, external_id="RC-CLOSED", published=datetime.now(UTC))
        s.get(Call, cid).deadline = datetime.now(UTC) - timedelta(days=1)
        s.commit()
    with pg_session_factory() as s:
        assert reconcile_missing_rankings(s, ctx=CTX) == 0


def _seed_failed_job(s, *, call_id: int, profile_id: int, updated_at: datetime) -> None:
    """Insert a terminally-FAILED (dead-lettered) ranking_job for the pair,
    with updated_at rewound to the given time."""
    job = RankingJob(
        call_id=call_id,
        profile_id=profile_id,
        status=RankingJobStatus.FAILED,
        attempts=5,
        last_error="dead-lettered",
    )
    s.add(job)
    s.flush()
    s.execute(update(RankingJob).where(RankingJob.id == job.id).values(updated_at=updated_at))


def test_reconcile_skips_pair_with_recent_dead_lettered_job(pg_session_factory, profile_factory):
    """A terminally-FAILED (dead-lettered) pair has no CallRanking and no
    active job, so the naive reconcile SELECT re-enqueues it every daily tick,
    burning attempts forever. A recent failed job must suppress re-enqueue."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call_with(
            s, external_id="RC-DL", published=datetime.now(UTC) - timedelta(days=1)
        )
        _seed_failed_job(
            s,
            call_id=call_id,
            profile_id=profile_id,
            updated_at=datetime.now(UTC) - timedelta(days=1),
        )
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s, ctx=CTX)
        s.commit()

    assert n == 0
    with pg_session_factory() as s:
        active = (
            s.execute(
                select(RankingJob).where(
                    RankingJob.status.in_([RankingJobStatus.PENDING, RankingJobStatus.RUNNING])
                )
            )
            .scalars()
            .all()
        )
    assert active == []  # not re-enqueued


def test_reconcile_re_enqueues_pair_with_old_dead_lettered_job(pg_session_factory, profile_factory):
    """An OLD failed job (past the suppression window) no longer blocks
    reconcile — the pair becomes eligible again so a long-ago transient
    failure isn't permanently abandoned."""
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        call_id = _seed_call_with(
            s, external_id="RC-DL-OLD", published=datetime.now(UTC) - timedelta(days=1)
        )
        _seed_failed_job(
            s,
            call_id=call_id,
            profile_id=profile_id,
            updated_at=datetime.now(UTC) - timedelta(days=30),
        )
        s.commit()

    with pg_session_factory() as s:
        n = reconcile_missing_rankings(s, ctx=CTX)
        s.commit()

    assert n == 1
    with pg_session_factory() as s:
        pending = (
            s.execute(
                select(RankingJob).where(
                    RankingJob.call_id == call_id,
                    RankingJob.profile_id == profile_id,
                    RankingJob.status == RankingJobStatus.PENDING,
                )
            )
            .scalars()
            .all()
        )
    assert len(pending) == 1


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


def test_max_attempts_single_source_of_truth():
    """B11 Defect 5: MAX_ATTEMPTS must be defined ONCE and shared, not
    duplicated across queue.py and worker.py where they can drift."""
    from grant_lab.ranking import queue as queue_mod
    from grant_lab.ranking import worker as worker_mod

    # worker.MAX_ATTEMPTS IS queue.MAX_ATTEMPTS — same object, no duplicate literal.
    assert worker_mod.MAX_ATTEMPTS is queue_mod.MAX_ATTEMPTS
    # The reaper's default also uses the shared constant.
    import inspect

    sig = inspect.signature(queue_mod.requeue_stuck_running)
    assert sig.parameters["max_attempts"].default == queue_mod.MAX_ATTEMPTS
