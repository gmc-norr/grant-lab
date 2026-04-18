"""Smoke tests for digest:weekly registration in build_scheduler."""
from unittest.mock import MagicMock

from call_monitor.scheduler import build_scheduler


class _NullEmail:
    def send(self, *, to, subject, html, text):
        raise NotImplementedError  # never called in scheduler smoke test


def _build(session_factory, *, with_digest):
    kwargs = dict(
        session_factory=session_factory,
        eu_portal_base_url="http://eu",
        vinnova_base_url="https://data.vinnova.se",
        alerter=MagicMock(),
        ollama_url="http://ollama",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    if with_digest:
        kwargs["digest_email_client"] = _NullEmail()
        kwargs["digest_settings"] = MagicMock(digest_dry_run=False, admin_dashboard_url="http://app/")
    return build_scheduler(**kwargs)


def test_scheduler_registers_digest_weekly_when_email_client_present(pg_session_factory):
    sched = _build(pg_session_factory, with_digest=True)
    job = sched.get_job("digest:weekly")
    assert job is not None
    assert job.name == "Weekly digest"
    sched.shutdown(wait=False)


def test_scheduler_skips_digest_weekly_without_email_client(pg_session_factory):
    sched = _build(pg_session_factory, with_digest=False)
    assert sched.get_job("digest:weekly") is None
    sched.shutdown(wait=False)
