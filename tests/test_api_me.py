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


def test_me_unauthenticated_is_envelope_401(monkeypatch, pg_url):
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
    assert body["user"]["status"] == "active"
    # Exact match on purpose: capabilities is a free-form dict, so an equality
    # check is what catches a new key being exposed to every client by accident.
    # retention_inspection is False here on BOTH counts — not an admin, and
    # RETENTION_INSPECTION_ENABLED defaults off.
    assert body["capabilities"] == {"admin": False, "retention_inspection": False}


def test_me_admin_capability(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="admin@ki.se", is_admin=True)
        s.add(u)
        s.commit()
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    r = c.get("/api/me")
    assert r.status_code == 200
    # retention_inspection stays False: this app is built without
    # RETENTION_INSPECTION_ENABLED, and admin alone does not grant it. Its
    # both-ways coverage lives in test_api_admin_retention_inspect.py.
    assert r.json()["capabilities"] == {"admin": True, "retention_inspection": False}


def test_me_exposes_user_name(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="named@ki.se", name="Dr. Named")
        s.add(u)
        s.commit()
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Dr. Named"


def test_me_name_is_null_when_unset(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="anon@ki.se")
        s.add(u)
        s.commit()
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["user"]["name"] is None
