from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import Subscription, User
from call_monitor.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _user(pg_session_factory, *, email="u@ki.se"):
    with pg_session_factory() as s:
        u = User(email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_me_lists_active_profiles_as_checkboxes(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    p1, _ = profile_factory(name="Group A")
    p2, _ = profile_factory(name="Group B")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.get("/me")
    assert r.status_code == 200
    assert "Group A" in r.text
    assert "Group B" in r.text
    assert 'name="subscribe_' in r.text


def test_me_post_subscribe_creates_rows(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    p1, _ = profile_factory(name="Group A")
    p2, _ = profile_factory(name="Group B")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        r = client.post(
            "/me",
            data={
                "name": "",  # existing name field stays
                f"subscribe_{p1}": "on",
                # p2 omitted → not subscribed
            },
        )
    assert r.status_code == 200
    with pg_session_factory() as s:
        subs = s.execute(select(Subscription).where(Subscription.user_id == uid)).scalars().all()
    assert len(subs) == 1
    assert subs[0].profile_id == p1


def test_me_post_unsubscribe_removes_row(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    p1, _ = profile_factory(name="Group A")
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory)
    with pg_session_factory() as s:
        s.add(Subscription(user_id=uid, profile_id=p1))
        s.commit()
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    with client:
        client.post("/me", data={"name": ""})  # no subscribe_ keys → unsubscribe all
    with pg_session_factory() as s:
        subs = s.execute(select(Subscription).where(Subscription.user_id == uid)).scalars().all()
    assert subs == []
