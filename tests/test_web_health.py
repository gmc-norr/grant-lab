from fastapi.testclient import TestClient

from call_manager.db.session import make_engine
from call_manager.main import create_app


# `migrated_engine` is requested by every test below: `pg_url` alone is an
# UNMIGRATED database, and the lifespan's stranded-reference sweep needs the
# `references` table. Verified 2026-09-02: this file failed when run alone.
def _set_min_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
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


def test_healthz_reports_a_missing_lock_without_failing(pg_url, migrated_engine, monkeypatch):
    """A standby (or a process whose lock backend died) is still alive for
    HTTP; the field, not the status code, carries the lock state."""
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        app.state.scheduler_lock = None
        r = client.get("/healthz")
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
