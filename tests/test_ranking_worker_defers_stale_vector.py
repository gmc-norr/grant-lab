"""Task 6 gates: the worker defers a pair whose vector has not caught up."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from grant_lab.db.models import CallRanking, Profile, RankingJob, RankingJobStatus
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


def test_a_missing_vector_does_not_defer_even_when_the_fingerprint_mismatches(
    session, session_factory, queued_pair, fake_rank_client
):
    """GATE 17, isolated onto the `embedding is not None` conjunct.

    The sibling above NULLs the fingerprint too, so it is carried entirely by
    `embedding_fingerprint is not None` -- which the deploy case already pins,
    and the spec's declared mutation for gate 17 ("treat NULL embedding as a
    mismatch") left it green. Here every OTHER conjunct votes "defer": the
    fingerprint is PRESENT and mismatched, the job is fresh. Only
    `profile.embedding is not None` can produce the proceed.

    ck_profiles_embedding_model_paired enforces
    (embedding IS NULL) = (embedding_model IS NULL), so the model goes with the
    vector; embedding_fingerprint is deliberately NOT in that constraint, so a
    present fingerprint over a NULL vector is a legal row.
    """
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding = None
    profile.embedding_model = None
    profile.embedding_fingerprint = "v1:deliberately_not_the_current_text"
    session.commit()

    with session_factory() as fresh:
        seeded = fresh.get(Profile, profile_id)
        assert seeded.embedding is None
        assert seeded.embedding_fingerprint == "v1:deliberately_not_the_current_text"
        assert seeded.embedding_fingerprint != embedding_fingerprint(seeded)

    drain_queue(
        session_factory=session_factory,
        client=fake_rank_client,
        embedding_threshold=0.0,
        batch_size=1,
    )

    with session_factory() as fresh:
        assert fresh.get(CallRanking, (call_id, profile_id)) is not None
        assert fresh.get(RankingJob, job_id).status == RankingJobStatus.DONE


def test_a_deferral_is_logged(session, session_factory, queued_pair, fake_rank_client, caplog):
    """The deferral must be observable: the spec wants a pair that defers
    repeatedly to read as an embedder problem, and nothing else in this file
    would notice a refactor that dropped the log."""
    call_id, profile_id, profile, job_id = queued_pair
    profile.embedding_fingerprint = "v1:deliberately_not_the_current_text"
    session.commit()

    with caplog.at_level(logging.INFO, logger="grant_lab.ranking.worker"):
        drain_queue(
            session_factory=session_factory,
            client=fake_rank_client,
            embedding_threshold=0.0,
            batch_size=1,
        )

    deferred = [
        r
        for r in caplog.records
        if "ranking.job_deferred_stale_vector" in r.getMessage()
        or getattr(r, "event", None) == "ranking_job_deferred_stale_vector"
    ]
    assert deferred, [r.getMessage() for r in caplog.records]
    assert any("ranking.job_deferred_stale_vector" in r.getMessage() for r in deferred)
    assert any(getattr(r, "event", None) == "ranking_job_deferred_stale_vector" for r in deferred)
    # The identifiers are what make a repeatedly-deferring pair diagnosable.
    assert any(getattr(r, "job_id", None) == job_id for r in deferred)
    assert any(getattr(r, "profile_id", None) == profile_id for r in deferred)

    # Guard the premise: this scenario really deferred.
    with session_factory() as fresh:
        assert fresh.get(RankingJob, job_id).status == RankingJobStatus.PENDING
        assert fresh.get(CallRanking, (call_id, profile_id)) is None
