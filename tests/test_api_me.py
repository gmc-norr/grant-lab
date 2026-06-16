from fastapi.testclient import TestClient

from call_manager.db.models import User
from call_manager.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.commit()
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def test_me_unauthenticated_is_envelope_401(monkeypatch, pg_url, pg_session_factory):
    c = TestClient(_app(monkeypatch, pg_url))
    r = c.get("/api/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_me_returns_user(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid = _login(app, pg_session_factory)
    r = c.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["id"] == uid
    assert body["user"]["email"] == "u@ki.se"
