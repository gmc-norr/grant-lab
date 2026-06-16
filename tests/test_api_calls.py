from fastapi.testclient import TestClient

from call_manager.db.models import Profile, ProfileKind, User
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
        s.flush()
        p = Profile(
            name="Grp",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=u.id,
        )
        s.add(p)
        s.commit()
        uid, pid = u.id, p.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid, pid


def test_api_calls_is_read_only(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory)
    r = c.get(f"/api/calls?profile={pid}")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "summary" in body and body["ranking"] == {"pending": 0, "failed": 0}
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None


def test_post_default_profile_persists(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, uid, pid = _login(app, pg_session_factory)
    r = c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver"},
        json={"profile_id": pid},
    )
    assert r.status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id == pid
    c.post(
        "/api/me/default-profile",
        headers={"origin": "http://testserver"},
        json={"profile_id": None},
    )
    with pg_session_factory() as s:
        assert s.get(User, uid).default_profile_id is None
