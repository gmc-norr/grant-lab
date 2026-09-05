"""The scheduler-tick that drives SubprocessRegistry.sweep_idle.

We don't actually run the scheduler — we verify that build_scheduler
registers a job with the right id and trigger, and that invoking the
job's closure calls registry.sweep_idle()."""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_build_scheduler_registers_idle_sweep_tick(monkeypatch, pg_url):
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


@pytest.mark.asyncio
async def test_idle_sweep_runs_on_app_loop_not_fresh_loop(monkeypatch, pg_url):
    """End-to-end H1: when build_scheduler is given an app_loop, the
    tick closure dispatches the sweep onto THAT loop via
    asyncio.run_coroutine_threadsafe — not a fresh loop spun up by
    asyncio.run(). This is the invariant that protects the registry's
    loop-bound asyncio.Lock and any loop-bound SDK client state from
    cross-loop corruption in production.

    We prove it by recording the loop the sweep coroutine actually ran
    on inside a probe sweep_idle, then comparing to the test's loop
    (which acts as the simulated FastAPI lifespan loop)."""
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

    # This is the loop that stands in for the FastAPI lifespan loop.
    app_loop = asyncio.get_running_loop()

    engine = make_engine(settings.database_url)
    try:
        session_factory = make_session_factory(engine)
        controller = build_workspace_controller(
            settings=settings,
            session_factory=session_factory,
        )

        # Probe: record the loop the sweep coroutine actually executes on.
        sweep_ran_on: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}

        async def probe_sweep() -> int:
            sweep_ran_on["loop"] = asyncio.get_running_loop()
            return 0

        controller.sweep_idle = probe_sweep  # type: ignore[method-assign]

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
            app_loop=app_loop,
            start=False,
        )
        try:
            job = sched.get_job("drafting_idle_sweep")
            assert job is not None
            # APScheduler invokes the closure from a worker thread. Simulate
            # that by running job.func() in run_in_executor. The closure
            # uses run_coroutine_threadsafe(coro, app_loop) and blocks on
            # fut.result(); meanwhile, this test's loop (== app_loop) is
            # free to process the scheduled coroutine because we're awaiting
            # the executor call.
            await asyncio.get_event_loop().run_in_executor(None, job.func)

            assert sweep_ran_on["loop"] is app_loop, (
                "sweep coroutine must run on the app_loop, not a fresh loop"
            )
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()


def test_idle_sweep_tick_also_reconciles(monkeypatch, pg_url):
    """The scheduled tick must run BOTH passes — otherwise reconciliation never
    runs in production."""
    from unittest.mock import MagicMock

    from grant_lab.config import Settings
    from grant_lab.db.session import make_engine, make_session_factory
    from grant_lab.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    settings = Settings()

    called = {"sweep": 0, "reconcile": 0}

    class _Ctrl:
        async def sweep_idle(self):
            called["sweep"] += 1
            return 0

        async def reconcile_live_clients(self):
            called["reconcile"] += 1
            return 0

    class _Alerter:
        def record_failure(self, source_slug, *, error=None):
            pass

        def record_success(self, source_slug):
            pass

    engine = make_engine(settings.database_url)
    try:
        session_factory = make_session_factory(engine)
        sched = build_scheduler(
            session_factory=session_factory,
            settings=settings,
            alerter=_Alerter(),
            ollama_url="http://localhost",
            gemma_model="gemma:test",
            ranking_tick_seconds=60,
            ranking_queue_batch=10,
            embedding_pregate_threshold=0.3,
            embed_client=MagicMock(model="bge-m3"),
            workspace_controller=_Ctrl(),
            start=False,
        )
        try:
            # app_loop is None here, so the closure drives its own asyncio.run.
            sched.get_job("drafting_idle_sweep").func()
            assert called == {"sweep": 1, "reconcile": 1}
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
