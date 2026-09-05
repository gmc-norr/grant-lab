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

from grant_lab.config import Settings
from grant_lab.scheduler import (
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
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")


# --- Defect 1: advisory-lock helper -----------------------------------------


class _FakeConn:
    """Minimal SQLAlchemy-connection stub: .execute(text(...)).scalar() ->
    the advisory-lock boolean, and .close() records closure."""

    def __init__(self, acquired: bool):
        self._acquired = acquired
        self.invalidated = False
        self.closed = False

    def execute(self, *_a, **_kw):
        result = MagicMock()
        result.scalar.return_value = self._acquired
        return result

    def close(self):
        self.closed = True

    def invalidate(self):
        self.invalidated = True


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
    assert engine.conn.invalidated is True
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


def test_release_actually_frees_advisory_lock_against_real_pg(pg_url):
    """release() must genuinely drop the session-level advisory lock so a FRESH
    connection can re-acquire it — WITHOUT disposing the engine.

    Regression guard: previously release() only called conn.close(), which on a
    pooled engine returns the connection to the pool (ROLLBACK, lock survives)
    rather than freeing the lock. A second instance would then be locked out
    until engine.dispose(). release() now runs pg_advisory_unlock first.
    """
    from sqlalchemy import create_engine, text

    # First engine takes the lock. Use a normal (pooled) engine so we exercise
    # the real pool-return-on-close behaviour the fix defends against.
    engine_a = create_engine(pg_url)
    # Second engine probes — must be a DIFFERENT pool/connection so it sees the
    # lock as held while A holds it, and as free after A releases.
    engine_b = create_engine(pg_url)
    try:
        lock = try_acquire_scheduler_lock(engine_a)
        assert isinstance(lock, SchedulerLock)

        # While A holds the lock, B cannot acquire it.
        with engine_b.connect() as conn_b:
            held = conn_b.execute(
                text("SELECT pg_try_advisory_lock(hashtext('call-manager-scheduler'))")
            ).scalar()
            # B failed to take it → release B's (no-op since it didn't get it)
            assert held is False, "lock should be held by engine_a while it's alive"

        # Release WITHOUT disposing engine_a. The pooled connection returns to
        # A's pool, but the advisory lock must already be dropped.
        lock.release()

        # A fresh connection on B must now be able to acquire the lock.
        with engine_b.connect() as conn_b:
            reacquired = conn_b.execute(
                text("SELECT pg_try_advisory_lock(hashtext('call-manager-scheduler'))")
            ).scalar()
            assert reacquired is True, (
                "release() did not free the advisory lock — a second instance "
                "would be locked out until engine.dispose()"
            )
            # Clean up: drop the lock B just took.
            conn_b.execute(text("SELECT pg_advisory_unlock(hashtext('call-manager-scheduler'))"))
    finally:
        engine_a.dispose()
        engine_b.dispose()


# --- Defect 2: executor sizing ----------------------------------------------


def _build(settings):
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

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


# --- Review 2026-09-02 §2.6(b): the probe names WHY, and a held lock can be lost


def test_probe_reports_acquired_held_elsewhere_and_unavailable():
    from grant_lab.scheduler import probe_scheduler_lock

    lock, state = probe_scheduler_lock(_FakeEngine(acquired=True))
    assert state == "acquired" and isinstance(lock, SchedulerLock)
    lock.release()

    lock, state = probe_scheduler_lock(_FakeEngine(acquired=False))
    assert (lock, state) == (None, "held_elsewhere")

    lock, state = probe_scheduler_lock(_FakeEngine(acquired=False, raise_on_connect=True))
    assert (lock, state) == (None, "unavailable")


def test_try_acquire_is_the_probe_without_the_reason():
    """The old entry point keeps its contract for every caller and test."""
    assert try_acquire_scheduler_lock(_FakeEngine(acquired=False)) is None
    lock = try_acquire_scheduler_lock(_FakeEngine(acquired=True))
    assert isinstance(lock, SchedulerLock)
    lock.release()


def test_is_held_goes_false_when_the_lock_backend_dies(pg_url, caplog):
    """Killing the lock backend post-boot used to be silent: the instance kept
    serving 200 with no log line while a second instance could take the lock
    (review A9 S5). is_held() pings the held connection; a dead session means
    the lock is gone, and that is logged exactly once."""
    import logging

    from sqlalchemy import create_engine, text

    engine_a = create_engine(pg_url)
    engine_b = create_engine(pg_url)
    try:
        lock = try_acquire_scheduler_lock(engine_a)
        assert isinstance(lock, SchedulerLock)
        assert lock.is_held() is True
        pid = lock._connection.execute(text("SELECT pg_backend_pid()")).scalar()
        with engine_b.connect() as conn_b:
            conn_b.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        with caplog.at_level(logging.WARNING):
            assert lock.is_held() is False
            assert lock.is_held() is False  # stays false, no second ping needed
        lost = [r for r in caplog.records if r.__dict__.get("event") == "scheduler_lock_lost"]
        assert len(lost) == 1, "the loss must be logged exactly once"
        assert lock.acquired is False
        # And the lock really is free for the next instance.
        with engine_b.connect() as conn_b:
            assert (
                conn_b.execute(
                    text("SELECT pg_try_advisory_lock(hashtext('call-manager-scheduler'))")
                ).scalar()
                is True
            )
            conn_b.execute(text("SELECT pg_advisory_unlock(hashtext('call-manager-scheduler'))"))
        lock.release()  # tolerant on a dead connection
    finally:
        engine_a.dispose()
        engine_b.dispose()
