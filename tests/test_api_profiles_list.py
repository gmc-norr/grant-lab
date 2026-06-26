from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="viewer@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_list_active_by_name(monkeypatch, pg_url, pg_session_factory, profile_factory):
    app = _setup(monkeypatch, pg_url)
    profile_factory(name="Beta", structured={"areas": ["a", "b"]})
    profile_factory(name="Alpha", structured={"areas": ["x"]})
    profile_factory(name="Archived")  # active by default; archive it below
    # archive the third
    from call_manager.db.models import Profile
    with pg_session_factory() as s:
        s.execute(Profile.__table__.select().where(Profile.name == "Archived")).first()
        archived_update = (
            Profile.__table__.update()
            .where(Profile.name == "Archived")
            .values(is_active=False)
        )
        s.execute(archived_update)
        s.commit()
    client = _login(app, pg_session_factory)
    with client:
        r = client.get("/api/profiles")
    assert r.status_code == 200
    body = r.json()
    names = [p["name"] for p in body["profiles"]]
    assert names == ["Alpha", "Beta"]  # active only, by name
    alpha = next(p for p in body["profiles"] if p["name"] == "Alpha")
    assert alpha["areas_count"] == 1
    assert alpha["kind"] == "organizational_unit"


def test_list_requires_auth(monkeypatch, pg_url):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/api/profiles")
    assert r.status_code == 401
