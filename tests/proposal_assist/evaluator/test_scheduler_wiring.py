import pytest

from grant_lab.config import Settings


@pytest.mark.asyncio
async def test_build_scheduler_registers_evaluator_ticks(monkeypatch, pg_url):
    from unittest.mock import MagicMock

    from grant_lab.db.session import make_engine, make_session_factory
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller
    from grant_lab.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    settings = Settings()
    engine = make_engine(settings.database_url)
    try:
        sf = make_session_factory(engine)
        controller = build_workspace_controller(settings=settings, session_factory=sf)

        class _Alerter:
            def record_failure(self, source_slug, *, error=None): ...
            def record_success(self, source_slug): ...

        sched = build_scheduler(
            session_factory=sf,
            settings=settings,
            alerter=_Alerter(),
            ollama_url="http://localhost",
            gemma_model="g",
            ranking_tick_seconds=60,
            ranking_queue_batch=10,
            embedding_pregate_threshold=0.3,
            embed_client=MagicMock(model="bge-m3"),
            workspace_controller=controller,
            start=False,
        )
        try:
            drain = sched.get_job("evaluator_drain")
            reaper = sched.get_job("evaluator_reaper")
            assert drain is not None
            assert drain.trigger.interval.total_seconds() == settings.evaluator_tick_seconds
            assert reaper is not None
            assert reaper.trigger.interval.total_seconds() == settings.evaluator_reaper_tick_seconds
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
