"""The scheduler-tick that drives SubprocessRegistry.sweep_idle.

We don't actually run the scheduler — we verify that build_scheduler
registers a job with the right id and trigger, and that invoking the
job's closure calls registry.sweep_idle()."""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_build_scheduler_registers_idle_sweep_tick(monkeypatch, pg_url):
    from unittest.mock import MagicMock

    from call_monitor.config import Settings
    from call_monitor.db.session import make_engine, make_session_factory
    from call_monitor.proposal_assist.drafting.factory import (
        build_workspace_controller,
    )
    from call_monitor.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
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

        # Replace sweep_idle with a tracker that records calls.
        sweep_calls = {"n": 0}
        original_sweep = controller.sweep_idle

        async def tracking_sweep():
            sweep_calls["n"] += 1
            return await original_sweep()

        controller.sweep_idle = tracking_sweep  # type: ignore[method-assign]

        class _Alerter:
            def record_failure(self, source_slug, *, error=None):
                pass

            def record_success(self, source_slug):
                pass

        alerter = _Alerter()
        embed_client = MagicMock(model="bge-m3")

        sched = build_scheduler(
            session_factory=session_factory,
            settings=settings,
            alerter=alerter,
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
            job = sched.get_job("drafting_idle_sweep")
            assert job is not None
            assert job.trigger.interval.total_seconds() == settings.workspace_idle_sweep_seconds
            # Invoke the closure once — should call sweep_idle().
            # Run in a thread so asyncio.run() inside the closure doesn't
            # collide with the pytest-asyncio event loop already running.
            await asyncio.get_event_loop().run_in_executor(None, job.func)
            # The closure uses asyncio.run internally; sweep_calls increments
            # synchronously by the time job.func() returns.
            assert sweep_calls["n"] >= 1
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
