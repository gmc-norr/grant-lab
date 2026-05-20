"""Smoke tests for digest:weekly registration in build_scheduler."""

from unittest.mock import MagicMock

import pytest

from call_manager.config import Settings
from call_manager.scheduler import build_scheduler


class _NullEmail:
    def send(self, *, to, subject, html, text):
        raise NotImplementedError  # never called in scheduler smoke test


@pytest.fixture
def _settings_env(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")


def _build(session_factory, *, with_digest):
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()
    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )
    kwargs = dict(
        session_factory=session_factory,
        settings=settings,
        alerter=MagicMock(),
        ollama_url="http://ollama",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    if with_digest:
        kwargs["digest_email_client"] = _NullEmail()
        kwargs["digest_settings"] = MagicMock(
            digest_dry_run=False, admin_dashboard_url="http://app/"
        )
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
