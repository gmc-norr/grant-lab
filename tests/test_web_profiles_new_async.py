from fastapi.testclient import TestClient

from call_manager.db.models import Profile, ProfileBootstrapJob, Subscription, User
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


def _client(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def test_create_does_no_llm_in_request_and_enqueues(monkeypatch, pg_url, pg_session_factory):
    def _boom(**_):
        raise AssertionError("bootstrap_profile must not run in the request")

    monkeypatch.setattr("call_manager.profiles.bootstrap.bootstrap_profile", _boom, raising=False)

    app = _app(monkeypatch, pg_url)
    c, uid = _client(app, pg_session_factory)
    r = c.post(
        "/profiles/new",
        data={"name": "Grp", "kind": "organizational_unit", "level": "group", "mission_text": "m"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/edit" in r.headers["location"]
    with pg_session_factory() as s:
        p = s.query(Profile).filter_by(name="Grp").one()
        assert p.description == ""
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=p.id, status="queued").count() == 1
        assert s.get(Subscription, (uid, p.id)) is not None
        assert s.get(User, uid).default_profile_id == p.id


def test_rejects_oversized_mission(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, _ = _client(app, pg_session_factory)
    r = c.post("/profiles/new", data={"name": "Grp", "mission_text": "x" * 10_241})
    assert r.status_code == 413


def test_rejects_too_many_members(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    c, _ = _client(app, pg_session_factory)
    many = "\n".join(f"0000-0002-1825-009{i % 10}" for i in range(51))
    r = c.post("/profiles/new", data={"name": "Grp", "orcids": many})
    assert r.status_code == 400
