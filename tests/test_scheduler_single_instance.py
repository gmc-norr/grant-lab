"""Defect-1 + Defect-2 hardening tests for the in-process scheduler.

Defect 1 (single-instance): a second app replica must NOT start its scheduler.
We enforce this with a Postgres SESSION-level advisory lock
(`pg_try_advisory_lock`). The lock acquisition is wrapped in a small testable
helper so we can assert the contract without a second live connection.

Defect 2 (worker starvation): the APScheduler thread pool must be sized well
above the default 10 so the ~8-10 deliberately-blocking LLM ticks can't starve
daily ingest / weekly digest.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from call_manager.config import Settings
from call_manager.scheduler import (
    SCHEDULER_EXECUTOR_MAX_WORKERS,
    SchedulerLock,
    build_scheduler,
    try_acquire_scheduler_lock,
)


class _NullAlerter:
    def record_failure(self, *a, **kw):
        pass

    def record_success(self, *a, **kw):
        pass


@pytest.fixture
def _settings_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")


# --- Defect 1: advisory-lock helper -----------------------------------------


class _FakeConn:
    """Minimal SQLAlchemy-connection stub: .execute(text(...)).scalar() ->
    the advisory-lock boolean, and .close() records closure."""

    def __init__(self, acquired: bool):
        self._acquired = acquired
        self.closed = False

    def execute(self, *_a, **_kw):
        result = MagicMock()
        result.scalar.return_value = self._acquired
        return result

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, acquired: bool, *, raise_on_connect: bool = False):
        self._acquired = acquired
        self._raise = raise_on_connect
        self.conn: _FakeConn | None = None

    def connect(self):
        if self._raise:
            raise RuntimeError("advisory locks unsupported")
        self.conn = _FakeConn(self._acquired)
        return self.conn


def test_try_acquire_returns_lock_when_acquired():
    engine = _FakeEngine(acquired=True)
    lock = try_acquire_scheduler_lock(engine)
    assert isinstance(lock, SchedulerLock)
    assert lock.acquired is True
    # The held connection must stay OPEN for the process lifetime.
    assert engine.conn is not None and engine.conn.closed is False
    # Releasing closes the connection.
    lock.release()
    assert engine.conn.closed is True


def test_try_acquire_returns_none_when_lock_held_by_another_instance():
    """When pg_try_advisory_lock returns False (another replica holds it),
    the helper returns None and does NOT leak the probing connection."""
    engine = _FakeEngine(acquired=False)
    lock = try_acquire_scheduler_lock(engine)
    assert lock is None
    # The probing connection must be closed — we didn't get the lock.
    assert engine.conn is not None and engine.conn.closed is True


def test_try_acquire_is_robust_when_engine_cannot_connect():
    """A DB that can't take advisory locks (or is unreachable) must not crash
    startup — the helper swallows the error and returns None."""
    engine = _FakeEngine(acquired=True, raise_on_connect=True)
    assert try_acquire_scheduler_lock(engine) is None


# --- Defect 2: executor sizing ----------------------------------------------


def _build(settings):
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )
    return build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )


def test_executor_pool_is_sized_above_default(_settings_env):
    """The known-blocking LLM ticks (~8-10 of them block a worker on
    fut.result(...)) must not be able to consume every worker and starve the
    daily ingest / weekly digest crons. The pool is sized well above the
    APScheduler default of 10."""
    sched = _build(Settings())
    assert SCHEDULER_EXECUTOR_MAX_WORKERS >= 30
    pool = sched._executors["default"]._pool
    assert pool._max_workers == SCHEDULER_EXECUTOR_MAX_WORKERS
    sched.shutdown(wait=False)
