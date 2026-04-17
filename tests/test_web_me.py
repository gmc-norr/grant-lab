from fastapi.testclient import TestClient

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.main import create_app


def _logged_in_client(monkeypatch, pg_url, pg_session_factory, *, email="alice@ki.se"):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        u = User(email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id

    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def test_me_requires_auth(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/me")
    assert r.status_code == 401


def test_me_renders_current_user(monkeypatch, pg_url, pg_session_factory):
    client, _ = _logged_in_client(monkeypatch, pg_url, pg_session_factory)
    with client:
        r = client.get("/me")
    assert r.status_code == 200
    assert "alice@ki.se" in r.text


def test_me_post_updates_name(monkeypatch, pg_url, pg_session_factory):
    client, uid = _logged_in_client(monkeypatch, pg_url, pg_session_factory)
    with client:
        r = client.post("/me", data={"name": "Alice Andersson"})
    assert r.status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.name == "Alice Andersson"
