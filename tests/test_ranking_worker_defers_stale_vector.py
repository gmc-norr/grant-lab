"""Task 6 gates: the worker defers a pair whose vector has not caught up."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from grant_lab.db.models import CallRanking, RankingJob, RankingJobStatus
from grant_lab.profiles.fingerprint import embedding_fingerprint
from grant_lab.ranking.worker import drain_queue
from tests.freshness_helpers import enqueue_job, make_call, make_profile, ollama_clean


@pytest.fixture()
def session_factory(pg_session_factory):
    return pg_session_factory


@pytest.fixture()
def session(pg_session_factory):
    with pg_session_factory() as s:
        yield s


@pytest.fixture()
def fake_rank_client():
    return ollama_clean()


@pytest.fixture()
def queued_pair(session):
    """(call_id, profile_id, profile, job_id) -- PENDING job, profile has a vector."""
    p = make_profile(session, description="Liver disease", structured={"areas": ["AI"]})
    p.embedding = [0.1] * 1024
    p.embedding_model = "bge-m3"
    p.embedding_fingerprint = embedding_fingerprint(p)
    c = make_call(session)
    job_id = enqueue_job(session, call_id=c.id, profile_id=p.id)
    session.commit()
    return c.id, p.id, p, job_id


def test_a_stale_vector_defers_without_burning_an_attempt(
    session, session_factory, queued_pair, fake_rank_client
):
    """GATE 15. Assert from a FRESH session -- an uncommitted revert would
    look correct from the worker's own session and be rolled back on close."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = "v1:deliberately_not_the_current_text"
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        job = fresh.get(RankingJob, job_id)
        assert job.status == RankingJobStatus.PENDING
        assert job.attempts == 0
        assert fresh.get(CallRanking, (call_id, profile_id)) is None


def test_a_missing_vector_does_not_defer(
    session, session_factory, queued_pair, fake_rank_client
):
    """GATE 17. A NULL vector is today's UNKNOWN-cosine path: the LLM
    adjudicates. Deferring here would stall ranking whenever Ollama is down."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding = None
    profile.embedding_model = None
    profile.embedding_fingerprint = None
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None


def test_a_null_fingerprint_with_a_present_vector_does_not_defer(
    session, session_factory, queued_pair, fake_rank_client
):
    """The deploy case. Every pre-0075 profile has a vector and a NULL
    fingerprint; deferring on that would pause ALL ranking until 03:30."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = None
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None


def test_deferral_is_bounded_by_job_age(
    session, session_factory, queued_pair, fake_rank_client
):
    """GATE 16. force=False preserves the old vector on an embedder outage, so
    the mismatch persists and the pair would defer forever."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = "v1:deliberately_not_the_current_text"
    job = session.get(RankingJob, job_id)
    job.created_at = datetime.now(UTC) - timedelta(hours=25)
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None


def test_a_current_vector_ranks_normally(
    session, session_factory, queued_pair, fake_rank_client
):
    """Discriminating control: without this, 'never defer' would pass gates
    16, 17 and the deploy case."""
    from grant_lab.profiles.fingerprint import embedding_fingerprint

    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = embedding_fingerprint(profile)
    session.commit()

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None
