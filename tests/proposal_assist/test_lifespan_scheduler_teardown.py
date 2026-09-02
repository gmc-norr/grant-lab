"""The lifespan must tear down the scheduler IT started, and must survive the
lock-not-acquired path.

Both defects were found by root-causing a wandering whole-suite failure: a zombie
APScheduler kept firing after the test that created it, and its
_analysis_drain_tick (one of the few that needs no app_loop) drained the shared
analysis queue out from under whichever tests/test_analysis_pipeline.py test
happened to be running. The same zombie also held the single-instance advisory
lock, which is what broke test_scheduler_single_instance.py in-suite.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from call_manager.main import create_app


@pytest.fixture
def _app(_settings_env, monkeypatch):
    """Both tests below enter a SECOND lifespan that cannot take the advisory
    lock. Since review 2026-09-02 §2.6(b) that is fatal by default; the standby
    flag keeps the tolerated build-but-do-not-start path these tests pin."""
    monkeypatch.setenv("SCHEDULER_STANDBY_OK", "true")
    return create_app()


def _live_scheduler_threads() -> list[str]:
    return [
        t.name for t in threading.enumerate() if t.is_alive() and "apscheduler" in t.name.lower()
    ]


def test_nested_lifespan_does_not_orphan_the_outer_scheduler(_app, pg_session_factory):
    """Entering a SECOND lifespan on the same app used to overwrite
    app.state.scheduler with a start=False one (the nested advisory-lock acquire
    necessarily loses to the outer, still-held lock) and app.state.scheduler_lock
    with None. Both teardowns then stopped that inert object, so the RUNNING
    scheduler survived the test and its lock was never released.

    The lifespan now closes over locals, so each teardown stops what it created.
    """
    _app.state.session_factory = pg_session_factory
    before = len(_live_scheduler_threads())

    outer = TestClient(_app)
    outer.__enter__()
    try:
        started = _app.state.scheduler
        assert started.running  # the outer really started one

        inner = TestClient(_app)
        inner.__enter__()
        # The nested build could not take the lock, so it is inert...
        assert _app.state.scheduler is not started
        assert not _app.state.scheduler.running
        inner.__exit__(None, None, None)

        # ...and shutting it down must NOT have stopped the outer's scheduler.
        assert started.running
    finally:
        outer.__exit__(None, None, None)

    assert not started.running  # stopped by ITS own lifespan
    assert len(_live_scheduler_threads()) == before  # no zombie left behind


def test_teardown_survives_the_lock_not_acquired_path(_app, pg_session_factory, monkeypatch):
    """Pins an EXISTING guarantee at the lifespan level rather than fixing a bug.

    The replica that loses the single-instance race builds the scheduler with
    start=False, and stock APScheduler raises SchedulerNotRunningError when
    shutdown() is called on a never-started scheduler — which would abort the
    lifespan's finally before the lock release, the drafting-subprocess
    termination and engine.dispose(). Nothing in the lifespan guards that; what
    saves it is scheduler._SafeBackgroundScheduler making shutdown() a no-op when
    STATE_STOPPED. This test fails if that subclass is ever swapped back for a
    plain BackgroundScheduler, which is the only reason it exists.
    """
    # The lifespan imports this INSIDE the function body, so it rebinds from
    # call_manager.scheduler on every entry — patch it there, not on main.
    from call_manager import scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module, "probe_scheduler_lock", lambda _e: (None, "held_elsewhere")
    )
    _app.state.session_factory = pg_session_factory

    client = TestClient(_app)
    client.__enter__()
    assert not _app.state.scheduler.running  # the not-started path is live
    client.__exit__(None, None, None)  # must not raise


def test_drafting_subprocesses_are_terminated_before_the_scheduler_wait(
    _app, pg_session_factory, monkeypatch
):
    """Review 2026-09-02 §2.6f: the teardown waited for scheduler ticks (bounded
    at up to ~16 minutes) BEFORE terminating drafting subprocesses, so a service
    manager that SIGKILLed first skipped the termination and leaked every SDK
    subprocess. The order is the property."""
    import uuid

    from call_manager import scheduler as scheduler_module

    # Standby path (no advisory lock → inert scheduler): a RUNNING scheduler's
    # registry-reconcile tick sees the fake entry below as a deleted workspace
    # and terminates it itself, racing this test. The order under test is the
    # teardown's own, and an inert scheduler still goes through shutdown().
    monkeypatch.setattr(
        scheduler_module, "probe_scheduler_lock", lambda _e: (None, "held_elsewhere")
    )
    _app.state.session_factory = pg_session_factory
    order: list[str] = []
    real_shutdown = scheduler_module._SafeBackgroundScheduler.shutdown

    def _shutdown(self, wait=True):
        order.append("scheduler.shutdown")
        return real_shutdown(self, wait=wait)

    monkeypatch.setattr(scheduler_module._SafeBackgroundScheduler, "shutdown", _shutdown)

    client = TestClient(_app)
    client.__enter__()
    ctrl = _app.state.workspace_controller

    async def _terminate(wsid):
        order.append("terminate")
        ctrl._registry._entries.pop(wsid, None)

    monkeypatch.setattr(ctrl, "terminate", _terminate)
    ctrl._registry._entries[uuid.uuid4()] = (
        object()
    )  # one live subprocess, as far as teardown knows
    client.__exit__(None, None, None)

    assert order == ["terminate", "scheduler.shutdown"], order
