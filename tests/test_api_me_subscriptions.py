from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Profile, ProfileKind, Subscription, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory):
    with pg_session_factory() as s:
        u = User(status="active", email="u@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def _profile(pg_session_factory, *, owner_id, name, active=True):
    with pg_session_factory() as s:
        p = Profile(name=name, kind=ProfileKind.ORGANIZATIONAL_UNIT.value, level="group",
                    description="d", structured={}, created_by=owner_id, is_active=active)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def test_subscriptions_diff_preserves_digest_enabled(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid_a = _profile(pg_session_factory, owner_id=uid, name="A")
    pid_b = _profile(pg_session_factory, owner_id=uid, name="B")
    # Pre-existing subscription to A with digest turned OFF — must survive a save that keeps A.
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=pid_a, digest_enabled=False))
        s.commit()
    with client:
        r = client.put("/api/me/subscriptions", json={"profile_ids": [pid_a, pid_b]})
    assert r.status_code == 200
    assert r.json() == {"subscribed": sorted([pid_a, pid_b])}
    with pg_session_factory() as s:
        row_a = s.execute(
            select(Subscription).where(
                Subscription.user_id == uid, Subscription.profile_id == pid_a
            )
        ).scalar_one()
        assert row_a.digest_enabled is False  # NOT reset to default True


def test_subscriptions_empty_clears(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    pid = _profile(pg_session_factory, owner_id=uid, name="A")
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=pid))
        s.commit()
    with client:
        r = client.put("/api/me/subscriptions", json={"profile_ids": []})
    assert r.status_code == 200
    assert r.json() == {"subscribed": []}
    with pg_session_factory() as s:
        assert s.execute(select(Subscription).where(Subscription.user_id == uid)).first() is None


def test_subscriptions_ignores_inactive(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory)
    archived = _profile(pg_session_factory, owner_id=uid, name="Z", active=False)
    with client:
        r = client.put("/api/me/subscriptions", json={"profile_ids": [archived]})
    assert r.status_code == 200
    assert r.json() == {"subscribed": []}
