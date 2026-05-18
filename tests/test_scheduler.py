from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from call_monitor.config import Settings
from call_monitor.scheduler import build_scheduler


class _NullAlerter:
    def record_failure(self, *a, **kw): pass
    def record_success(self, *a, **kw): pass


@pytest.fixture
def _settings_env(monkeypatch):
    """Populate the env vars Settings() requires.

    Source base_urls fall back to the Settings defaults when EU_PORTAL_BASE_URL /
    VINNOVA_BASE_URL are unset, which is fine for these smoke tests — we don't
    actually start the scheduler.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def test_build_scheduler_returns_one_job_per_active_adapter(_settings_env):
    """Smoke: the scheduler is constructed with a job per source. We don't
    actually start it — that would fire HTTP requests."""
    sched = build_scheduler(
        session_factory=lambda: None,  # not invoked because we don't start
        settings=Settings(),
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    assert isinstance(sched, BackgroundScheduler)
    actual_ids = {j.id for j in sched.get_jobs()}
    expected_required = {"ingest:eu_portal", "ingest:vinnova", "rank:drain",
                         "golden_eval:weekly",
                         "embeddings:refresh-missing",
                         "ranking:reconcile"}
    assert expected_required.issubset(actual_ids)
    sched.shutdown(wait=False)


def test_build_scheduler_includes_rate_limit_sweep_when_limiters_given(_settings_env):
    """The in-memory rate limiter's `_events` dict grows with every unique
    (email|ip) key. Without a periodic sweep, a long-running process with
    turnover in failed-login attempts will leak memory indefinitely. The
    scheduler wires up a sweep job when callers hand it the limiters to
    sweep."""
    from call_monitor.auth.rate_limit import SlidingWindowLimiter

    email = SlidingWindowLimiter(max_requests=5, window_seconds=60, name="email")
    ip = SlidingWindowLimiter(max_requests=30, window_seconds=60, name="ip")

    sched = build_scheduler(
        session_factory=lambda: None,
        settings=Settings(),
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        limiters=[email, ip],
        start=False,
    )
    job_ids = {j.id for j in sched.get_jobs()}
    assert "rate_limit:sweep" in job_ids
    sched.shutdown(wait=False)


def test_build_scheduler_omits_rate_limit_sweep_when_limiters_empty(_settings_env):
    """No limiters ⇒ no sweep job. Mirrors digest's opt-in pattern so
    unit tests that don't need the sweep don't accumulate it."""
    sched = build_scheduler(
        session_factory=lambda: None,
        settings=Settings(),
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    job_ids = {j.id for j in sched.get_jobs()}
    assert "rate_limit:sweep" not in job_ids
    sched.shutdown(wait=False)


def test_build_scheduler_includes_rank_drain_job(_settings_env):
    sched = build_scheduler(
        session_factory=lambda: None,
        settings=Settings(),
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    job_ids = sorted(j.id for j in sched.get_jobs())
    assert "rank:drain" in job_ids
    sched.shutdown(wait=False)
