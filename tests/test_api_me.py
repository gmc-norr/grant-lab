from grant_lab.db.models import User
from grant_lab.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _login(app, pg_session_factory, test_client_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@umu.se")
        s.add(u)
        s.commit()
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def test_me_unauthenticated_is_envelope_401(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    c = test_client_factory(_app(monkeypatch, pg_url))
    r = c.get("/api/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_me_returns_user(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _app(monkeypatch, pg_url)
    c, uid = _login(app, pg_session_factory, test_client_factory)
    r = c.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["id"] == uid
    assert body["user"]["email"] == "u@umu.se"
    assert body["user"]["status"] == "active"
    # Exact match on purpose: capabilities is a free-form dict, so an equality
    # check is what catches a new key being exposed to every client by accident.
    assert body["capabilities"] == {"admin": False}


def test_me_admin_capability(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="admin@umu.se", is_admin=True)
        s.add(u)
        s.commit()
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["capabilities"] == {"admin": True}


def test_me_exposes_user_name(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="named@umu.se", name="Dr. Named")
        s.add(u)
        s.commit()
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["user"]["name"] == "Dr. Named"


def test_me_name_is_null_when_unset(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="anon@umu.se")
        s.add(u)
        s.commit()
        uid = u.id
    c = test_client_factory(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["user"]["name"] is None
