from sqlalchemy import select

from call_monitor.db.models import Call, RankingJob, RankingJobStatus, Source
from call_monitor.ranking.queue import (
    claim_pending_jobs,
    enqueue_for_pair,
    mark_done,
    mark_failed,
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
