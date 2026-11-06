"""Smoke tests for digest:weekly registration in build_scheduler."""

from unittest.mock import MagicMock

import pytest

from grant_lab.config import Settings
from grant_lab.scheduler import build_scheduler


class _NullEmail:
    def send(self, *, to, subject, html, text):
        raise NotImplementedError  # never called in scheduler smoke test


@pytest.fixture
def _settings_env(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def _build(session_factory, *, with_digest):

    settings = Settings().model_copy(
        update={
            "ollama_url": "http://ollama",
            "gemma_model": "gemma4:e4b-q8",
            "ranking_tick_seconds": 60,
            "ranking_queue_batch": 10,
            "embedding_pregate_threshold": 0.30,
            "embedding_hard_fail_threshold": None,
            "anthropic_api_key": None,
            "digest_dry_run": False,
            "admin_dashboard_url": "http://app/",
        }
    )
    kwargs = dict(
        session_factory=session_factory,
        settings=settings,
        alerter=MagicMock(),
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    if with_digest:
        kwargs["digest_email_client"] = _NullEmail()
    return build_scheduler(**kwargs)


def test_scheduler_registers_digest_weekly_when_email_client_present(
    pg_session_factory,
    _settings_env,
):
    sched = _build(pg_session_factory, with_digest=True)
    job = sched.get_job("digest:weekly")
    assert job is not None
    assert job.name == "Weekly digest"
    sched.shutdown(wait=False)


def test_scheduler_skips_digest_weekly_without_email_client(
    pg_session_factory,
    _settings_env,
):
    sched = _build(pg_session_factory, with_digest=False)
    assert sched.get_job("digest:weekly") is None
    sched.shutdown(wait=False)


def test_digest_job_sets_generous_misfire_grace_time(
    pg_session_factory,
    _settings_env,
):
    """APScheduler's default misfire_grace_time is 1s, so a restart/GC at the
    Monday 07:00 fire instant would silently drop the whole week's digest.
    The digest job must set an explicit, generous grace (the DigestRun
    week-uniqueness guard makes a late run safe)."""
    sched = _build(pg_session_factory, with_digest=True)
    job = sched.get_job("digest:weekly")
    assert job is not None
    # Must be explicitly set and far larger than the 1-second default.
    assert job.misfire_grace_time is not None
    assert job.misfire_grace_time >= 6 * 3600
    sched.shutdown(wait=False)
