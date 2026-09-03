from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@umu.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def test_update_name_strips(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/me/name", json={"name": "  Pär  "})
    assert r.status_code == 200
    assert r.json() == {"name": "Pär"}
    with pg_session_factory() as s:
        assert s.get(User, uid).name == "Pär"


def test_update_name_empty_becomes_null(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/me/name", json={"name": "   "})
    assert r.status_code == 200
    assert r.json() == {"name": None}


def test_update_name_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/api/me/name", json={"name": "x"})
    assert r.status_code == 401


def test_update_name_too_long_returns_422(monkeypatch, pg_url, pg_session_factory):
    """Defect 2: a name >200 chars must produce a clean 422, not a 500 DataError."""
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory)
    with client:
        r = client.post("/api/me/name", json={"name": "A" * 201})
    assert r.status_code == 422
