from fastapi.testclient import TestClient

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="u@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_profiles_list_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/profiles")
    assert r.status_code == 401


def test_profiles_list_shows_active_only(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    p_active, _ = profile_factory(name="Active group")
    p_inactive, _ = profile_factory(name="Inactive group")
    from call_monitor.db.models import Profile
    with pg_session_factory() as s:
        s.get(Profile, p_inactive).is_active = False
        s.commit()

    app = _setup(monkeypatch, pg_url)
    client = _logged_in(app, pg_session_factory)
    with client:
        r = client.get("/profiles")
    assert r.status_code == 200
    assert "Active group" in r.text
    assert "Inactive group" not in r.text
