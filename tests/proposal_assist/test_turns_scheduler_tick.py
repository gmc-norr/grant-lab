"""The two new B4 scheduler jobs: turn_drain + compaction_tick.

Verifies build_scheduler registers both with the right ids/intervals."""

import pytest


@pytest.mark.asyncio
async def test_build_scheduler_registers_turn_drain_and_compaction_ticks(
    monkeypatch,
    pg_url,
):
    from unittest.mock import MagicMock

    from grant_lab.config import Settings
    from grant_lab.db.session import make_engine, make_session_factory
    from grant_lab.proposal_assist.drafting.factory import (
        build_workspace_controller,
    )
    from grant_lab.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    settings = Settings()

    engine = make_engine(settings.database_url)
    try:
        session_factory = make_session_factory(engine)
        controller = build_workspace_controller(
            settings=settings,
            session_factory=session_factory,
        )

        class _Alerter:
            def record_failure(self, source_slug, *, error=None):
                pass

            def record_success(self, source_slug):
                pass

        embed_client = MagicMock(model="bge-m3")
        sched = build_scheduler(
            session_factory=session_factory,
            settings=settings,
            alerter=_Alerter(),
            ollama_url="http://localhost",
            gemma_model="gemma:test",
            ranking_tick_seconds=60,
            ranking_queue_batch=10,
            embedding_pregate_threshold=0.3,
            embed_client=embed_client,
            workspace_controller=controller,
            start=False,
        )
        try:
            drain_job = sched.get_job("turn_drain")
            assert drain_job is not None
            assert drain_job.trigger.interval.total_seconds() == settings.turn_drain_tick_seconds
            comp_job = sched.get_job("compaction_tick")
            assert comp_job is not None
            assert comp_job.trigger.interval.total_seconds() == settings.compaction_tick_seconds
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
