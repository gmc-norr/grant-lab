"""RANKING_LOGPROBS_ENABLED must reach the ranker, not just exist.

The setting crosses three hops — Settings -> the scheduler's rank:drain job ->
drain_queue -> rank_pair -> the Ollama request. Tests that drive `rank_pair`
directly (tests/test_ranking_rank_pair.py) stay green on an app that never
passes the flag at all, so the hops get their own tests here.
"""

from __future__ import annotations

import pytest

import grant_lab.scheduler as scheduler_mod
from grant_lab.config import Settings


class _NullAlerter:
    def record_failure(self, *a, **kw):
        return None

    def record_success(self, *a, **kw):
        return None


@pytest.fixture()
def _settings_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")


class _FakeEmbedder:
    model = "fake-embed"

    def embed(self, text: str):
        return [0.1] * 1024


def _run_drain_job(monkeypatch, pg_session_factory, settings) -> dict:
    """Build the real scheduler, take its rank:drain closure, run it, and
    return the kwargs it handed to drain_queue."""
    captured: dict = {}

    def _fake_drain(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(scheduler_mod, "drain_queue", _fake_drain)

    sched = scheduler_mod.build_scheduler(
        session_factory=pg_session_factory,
        settings=settings.model_copy(
            update={
                "ollama_url": "http://localhost:11434",
                "gemma_model": "gemma4:e4b-q8",
                "ranking_tick_seconds": 60,
                "ranking_queue_batch": 10,
                "embedding_pregate_threshold": 0.30,
                "embedding_hard_fail_threshold": None,
                "anthropic_api_key": None,
            }
        ),
        alerter=_NullAlerter(),
        embed_client=_FakeEmbedder(),
        start=False,
    )
    try:
        job = sched.get_job("rank:drain")
        assert job is not None, "rank:drain job is missing from the scheduler"
        job.func()
    finally:
        sched.shutdown(wait=False)
    assert captured, "the rank:drain job never called drain_queue"
    return captured


def test_the_drain_job_defaults_to_logprobs_off(monkeypatch, pg_session_factory, _settings_env):
    captured = _run_drain_job(monkeypatch, pg_session_factory, Settings())

    assert captured["logprobs_enabled"] is False


def test_the_drain_job_carries_the_setting_through(monkeypatch, pg_session_factory, _settings_env):
    """The other direction. Without this, a hop that hard-coded False would
    pass the test above and silently make the setting unusable."""
    monkeypatch.setenv("RANKING_LOGPROBS_ENABLED", "true")

    captured = _run_drain_job(monkeypatch, pg_session_factory, Settings())

    assert captured["logprobs_enabled"] is True


def test_drain_queue_hands_the_flag_to_rank_pair(monkeypatch, pg_session_factory):
    """The remaining hop: drain_queue -> rank_pair.

    Driven directly rather than through the scheduler, because the scheduler
    test above stubs drain_queue out and therefore cannot see inside it.
    """
    from grant_lab.db.models import RankingJob
    from grant_lab.ranking import worker as worker_mod
    from tests.freshness_helpers import make_call, make_profile

    seen: list[bool] = []

    def _fake_rank_pair(**kwargs):
        seen.append(kwargs["logprobs_enabled"])
        raise RuntimeError("stop here — the kwarg is what is under test")

    monkeypatch.setattr(worker_mod, "rank_pair", _fake_rank_pair)

    with pg_session_factory() as session:
        profile = make_profile(session)
        call = make_call(session, deadline=None)
        session.add(RankingJob(call_id=call.id, profile_id=profile.id, status="pending"))
        session.commit()

    for enabled in (False, True):
        seen.clear()
        worker_mod.drain_queue(
            session_factory=pg_session_factory,
            client=object(),
            embedding_threshold=0.30,
            logprobs_enabled=enabled,
        )
        with pg_session_factory() as session:
            for job in session.query(RankingJob).all():
                job.status = "pending"
                job.attempts = 0
            session.commit()
        assert seen == [enabled], f"drain_queue did not pass logprobs_enabled={enabled}"
