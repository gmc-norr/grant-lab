# tests/test_web_login_google_button.py
from fastapi.testclient import TestClient

from call_manager.main import create_app


def _env(monkeypatch, pg_url, enabled):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    if enabled:
        monkeypatch.setenv("GOOGLE_OIDC_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    return create_app()


def test_button_shown_when_enabled(monkeypatch, pg_url, pg_session_factory):
    app = _env(monkeypatch, pg_url, enabled=True)
    with TestClient(app) as c:
        assert "/auth/google/start" in c.get("/login").text


def test_button_hidden_when_disabled(monkeypatch, pg_url, pg_session_factory):
    app = _env(monkeypatch, pg_url, enabled=False)
    with TestClient(app) as c:
        assert "/auth/google/start" not in c.get("/login").text
