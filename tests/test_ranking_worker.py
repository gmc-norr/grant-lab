from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    RankingJob,
    RankingJobStatus,
    Source,
)
from call_monitor.ranking.ollama_client import LLMError
from call_monitor.ranking.queue import enqueue_for_pair
from call_monitor.ranking.rank_pair import RankResult
from call_monitor.ranking.worker import MAX_ATTEMPTS, drain_queue


class _StaticRanker:
    """Test double for rank_pair — returns a canned RankResult."""

    def __init__(self, score: int = 75):
        self.calls = 0
        self.score = score

    def __call__(self, *, client, profile, call):
        self.calls += 1
        return RankResult(
            score=self.score,
            rationale=f"Static {self.score}",
            model="gemma4:e4b-q8",
            prompt_version="v1",
            raw_response={"score": self.score},
        )


class _AlwaysFailingRanker:
    def __init__(self):
        self.calls = 0

    def __call__(self, *, client, profile, call):
        self.calls += 1
        raise LLMError("simulated outage")


def _seed_call_and_pending_job(s, profile_id):
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(source_id=src.id, external_id="W-1", url="https://x", title="T")
    s.add(c)
    s.flush()
    enqueue_for_pair(s, call_id=c.id, profile_id=profile_id)
    s.commit()
    return c.id


def test_drain_runs_pending_jobs_and_writes_rankings(
    pg_session_factory, profile_factory
):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        _seed_call_and_pending_job(s, profile_id)

    ranker = _StaticRanker(score=42)
    n = drain_queue(
        session_factory=pg_session_factory,
        client=None,  # ranker doesn't use it
        ranker=ranker,
        batch_size=10,
    )
    assert n == 1
    assert ranker.calls == 1

    with pg_session_factory() as s:
        rows = s.execute(select(CallRanking)).scalars().all()
        jobs = s.execute(select(RankingJob)).scalars().all()
    assert len(rows) == 1
    assert rows[0].score == 42
    assert rows[0].rationale == "Static 42"
    assert jobs[0].status == RankingJobStatus.DONE


def test_drain_retries_on_llm_error_then_dead_letters(
    pg_session_factory, profile_factory
):
    profile_id, _ = profile_factory()
    with pg_session_factory() as s:
        _seed_call_and_pending_job(s, profile_id)

    ranker = _AlwaysFailingRanker()
    # Run drain MAX_ATTEMPTS times; each tick claims the still-pending job.
    for _ in range(MAX_ATTEMPTS):
        drain_queue(
            session_factory=pg_session_factory,
            client=None,
            ranker=ranker,
            batch_size=10,
        )

    with pg_session_factory() as s:
        job = s.execute(select(RankingJob)).scalar_one()
    assert job.attempts == MAX_ATTEMPTS
    assert job.status == RankingJobStatus.FAILED
    assert "simulated outage" in job.last_error


def test_drain_skips_when_no_pending_jobs(pg_session_factory):
    n = drain_queue(
        session_factory=pg_session_factory,
        client=None,
        ranker=_StaticRanker(),
        batch_size=10,
    )
    assert n == 0
