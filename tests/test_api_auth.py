from fastapi.testclient import TestClient

from call_manager.main import create_app


def _app(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        **extra,
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_config_reports_domains_and_google_disabled(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["google_oidc_enabled"] is False
    assert body["allowed_domains"] == ["ki.se"]


def test_config_reports_google_enabled(monkeypatch, pg_url, pg_session_factory):
    app = _app(
        monkeypatch,
        pg_url,
        GOOGLE_OIDC_ENABLED="true",
        GOOGLE_CLIENT_ID="cid",
        GOOGLE_CLIENT_SECRET="sec",
    )
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.json()["google_oidc_enabled"] is True


def test_config_is_public_no_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert "set-cookie" not in {k.lower() for k in r.headers}
