from fastapi.testclient import TestClient

from grant_lab.db.session import make_engine
from grant_lab.main import create_app


# `migrated_engine` is requested by every test below: `pg_url` alone is an
# UNMIGRATED database, and the lifespan's stranded-reference sweep needs the
# `references` table. Verified 2026-09-02: this file failed when run alone.
def _set_min_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def test_healthz_returns_ok(pg_url, migrated_engine, monkeypatch):
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    # Review 2026-09-02 §2.6(b): a monitor must be able to see which process
    # holds the single-instance lock. This app booted alone, so it does.
    assert r.json() == {"status": "ok", "scheduler_lock": True}


class _LostLock:
    """A SchedulerLock whose backend died: the watchdog has stopped the
    scheduler (93a5ea26), so this process serves HTTP and runs no jobs."""

    def is_held(self) -> bool:
        return False


def test_healthz_is_503_after_this_process_loses_the_lock(pg_url, migrated_engine, monkeypatch):
    """Review 2026-10-06 M12 (carried from the 2026-09-05 runtime-ownership
    review). Since the watchdog stops the scheduler on lock loss, a 200 here
    meant a process doing zero scheduled work while every status-code monitor
    stayed green. This test used to pin that 200."""
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        real = app.state.scheduler_lock
        app.state.scheduler_lock = _LostLock()
        try:
            r = client.get("/healthz")
        finally:
            app.state.scheduler_lock = real
    assert r.status_code == 503
    assert r.json() == {"status": "scheduler_lock_not_held", "scheduler_lock": False}


def test_healthz_is_503_when_the_lock_was_never_acquired(pg_url, migrated_engine, monkeypatch):
    """The 'unavailable' boot path: the lock backend could not be probed, so no
    scheduler started. Once the database is back the DB ping passes, and only
    the lock state says this process needs a restart."""
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        real = app.state.scheduler_lock
        app.state.scheduler_lock = None
        try:
            r = client.get("/healthz")
        finally:
            app.state.scheduler_lock = real
    assert r.status_code == 503
    assert r.json()["scheduler_lock"] is False


def test_a_deliberate_standby_without_the_lock_is_still_200(pg_url, migrated_engine, monkeypatch):
    """SCHEDULER_STANDBY_OK=true is an HTTP-only standby by design: healthy for
    HTTP, and the field says it holds no lock."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("SCHEDULER_STANDBY_OK", "true")
    app = create_app()
    with TestClient(app) as client:
        real = app.state.scheduler_lock
        app.state.scheduler_lock = None
        try:
            r = client.get("/healthz")
        finally:
            app.state.scheduler_lock = real
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "scheduler_lock": False}


def test_healthz_returns_503_when_db_unreachable(pg_url, migrated_engine, monkeypatch):
    """Outage path: simulate a dead database by swapping app.state.engine."""
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    live_engine = app.state.engine
    # Replace the engine with one pointing at a closed port (fast TCP refusal).
    app.state.engine = make_engine("postgresql+psycopg://x:x@127.0.0.1:1/x")
    try:
        with TestClient(app) as client:
            r = client.get("/healthz")
    finally:
        live_engine.dispose()
    assert r.status_code == 503
    assert "db unavailable" in r.json()["detail"]
