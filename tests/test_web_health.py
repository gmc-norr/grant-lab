from fastapi.testclient import TestClient

from call_monitor.db.session import make_engine
from call_monitor.main import create_app


def _set_min_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def test_healthz_returns_ok(pg_url, monkeypatch):
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_returns_503_when_db_unreachable(pg_url, monkeypatch):
    """Outage path: simulate a dead database by swapping app.state.engine."""
    _set_min_env(monkeypatch, pg_url)
    app = create_app()
    # Replace the engine with one pointing at a closed port (fast TCP refusal).
    app.state.engine = make_engine("postgresql+psycopg://x:x@127.0.0.1:1/x")
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 503
    assert "db unavailable" in r.json()["detail"]
