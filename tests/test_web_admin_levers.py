from datetime import UTC, datetime

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


def _admin_client(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="root@ki.se", is_admin=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_reenable_clears_digest_disabled_at(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        bounced = User(email="bounced@ki.se", digest_disabled_at=datetime.now(UTC))
        s.add(bounced)
        s.commit()
        s.refresh(bounced)
        target_id = bounced.id

    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": str(target_id)},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?ok=reenable"
    with pg_session_factory() as s:
        refreshed = s.get(User, target_id)
        assert refreshed.digest_disabled_at is None


def test_reenable_already_enabled_is_noop_ok(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        enabled = User(email="fine@ki.se", digest_disabled_at=None)
        s.add(enabled)
        s.commit()
        s.refresh(enabled)
        target_id = enabled.id

    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": str(target_id)},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin?ok=reenable"


def test_reenable_unknown_user_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    with client:
        r = client.post(
            "/admin/digest/reenable",
            data={"user_id": "999999"},
            follow_redirects=False,
        )
    assert r.status_code == 404


def test_reenable_requires_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="nonadmin@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post("/admin/digest/reenable", data={"user_id": "1"})
    assert r.status_code == 403
