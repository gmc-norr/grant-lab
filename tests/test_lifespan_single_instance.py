"""Review 2026-09-02 §2.6(b) / Appendix A9 F1: a second process was tolerated.

`uvicorn --workers 2` against one database booted both workers, took ONE advisory
lock, logged one warning, and served 200 on both. Only the scheduler was guarded;
the collab room manager, drafting registry, pass registry, lifecycle tombstones
and login limiters are per-process, so two collaborators on different workers got
two Yjs docs that never met. And the lifespan mutated shared state BEFORE it even
probed the lock: the stranded-reference sweep would reset the first instance's
in-flight ingests to 'failed'.

Now the lock is probed first, and losing it to another process is fatal unless
`SCHEDULER_STANDBY_OK=true`.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from grant_lab.main import create_app
from grant_lab.scheduler import SingleInstanceViolation, probe_scheduler_lock


# `migrated_engine` is requested by every test below: `pg_url` alone is an
# UNMIGRATED database, and the lifespan's stranded-reference sweep needs the
# `references` table. Verified 2026-09-02: this file failed when run alone.
def _set_min_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def test_a_second_instance_refuses_to_boot_by_default(pg_url, migrated_engine, monkeypatch, caplog):
    _set_min_env(monkeypatch, pg_url)
    first = create_app()
    with TestClient(first) as client_a:
        assert client_a.get("/healthz").json()["scheduler_lock"] is True
        second = create_app()
        try:
            with caplog.at_level(logging.ERROR), pytest.raises(SingleInstanceViolation):
                TestClient(second).__enter__()
        finally:
            second.state.engine.dispose()
        events = [r.__dict__.get("event") for r in caplog.records]
        assert "scheduler_lock_held_elsewhere_fatal" in events
        # The first instance is untouched: still holds the lock, still serves.
        assert first.state.scheduler.running
        assert client_a.get("/healthz").json() == {"status": "ok", "scheduler_lock": True}


def test_standby_flag_boots_without_the_scheduler_and_says_so(
    pg_url, migrated_engine, monkeypatch, caplog
):
    _set_min_env(monkeypatch, pg_url)
    first = create_app()
    with TestClient(first):
        monkeypatch.setenv("SCHEDULER_STANDBY_OK", "true")
        standby = create_app()
        with caplog.at_level(logging.WARNING), TestClient(standby) as client_b:
            assert standby.state.scheduler_lock is None
            assert not standby.state.scheduler.running
            # Visible to a monitor: the standby says it does not hold the lock.
            assert client_b.get("/healthz").json() == {"status": "ok", "scheduler_lock": False}
        warned = [
            r for r in caplog.records if r.__dict__.get("event") == "scheduler_lock_not_acquired"
        ]
        assert warned and warned[0].__dict__.get("lock_state") == "held_elsewhere"


def test_an_unavailable_lock_backend_keeps_the_tolerant_path(
    pg_url, migrated_engine, monkeypatch, caplog
):
    """A database that cannot answer the probe is an outage, not a second
    instance: boot with a warning and no scheduler (as before), and let
    /healthz's own DB check report the outage."""
    from grant_lab import scheduler as scheduler_module

    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setattr(scheduler_module, "probe_scheduler_lock", lambda _e: (None, "unavailable"))
    app = create_app()
    with caplog.at_level(logging.WARNING), TestClient(app):
        assert not app.state.scheduler.running
    warned = [r for r in caplog.records if r.__dict__.get("event") == "scheduler_lock_not_acquired"]
    assert warned and warned[0].__dict__.get("lock_state") == "unavailable"


def test_a_startup_failure_after_the_lock_releases_it(pg_url, migrated_engine, monkeypatch):
    """The lock is taken FIRST, so a later startup step that raises must give
    it back — otherwise the next boot in this process (or the next test) is
    refused as a second instance."""
    from grant_lab import admin_bootstrap

    _set_min_env(monkeypatch, pg_url)

    def _boom(*a, **k):
        raise RuntimeError("bootstrap exploded")

    monkeypatch.setattr(admin_bootstrap, "bootstrap_admins", _boom)
    app = create_app()
    try:
        with pytest.raises(RuntimeError, match="bootstrap exploded"):
            TestClient(app).__enter__()
        lock, state = probe_scheduler_lock(app.state.engine)
        try:
            assert state == "acquired", "the failed boot left the lock held"
        finally:
            if lock is not None:
                lock.release()
    finally:
        app.state.engine.dispose()


def test_a_nested_lifespan_on_the_same_app_is_not_a_second_instance(
    pg_url, migrated_engine, monkeypatch
):
    """Two TestClients on ONE app object (the pattern most route tests use) enter
    the lifespan twice in one process. The outer already holds the lock, so the
    inner reuses it: no refusal, no second scheduler, and /healthz still says
    True because app.state keeps the outer's live lock."""
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as outer:
        started = app.state.scheduler
        with TestClient(app) as inner:
            assert app.state.scheduler is not started
            assert not app.state.scheduler.running
            assert inner.get("/healthz").json() == {"status": "ok", "scheduler_lock": True}
        assert started.running
        assert outer.get("/healthz").json() == {"status": "ok", "scheduler_lock": True}
