from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Call, Source, User, UserCallDismissal
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _logged_in(app, pg_session_factory):
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


def _seed_call(pg_session_factory, *, title="HideMe"):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=title,
            url="https://x",
            title=title,
            published_at=datetime.now(UTC),
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def test_hide_post_creates_dismissal_and_redirects(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _logged_in(app, pg_session_factory)
    with client:
        r = client.post(f"/calls/{cid}/hide", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    with pg_session_factory() as s:
        d = s.execute(
            select(UserCallDismissal).where(
                UserCallDismissal.user_id == uid, UserCallDismissal.call_id == cid
            )
        ).scalar_one()
    assert d.dismissed_at is not None


def test_hide_is_idempotent(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid = _logged_in(app, pg_session_factory)
    with client:
        client.post(f"/calls/{cid}/hide", follow_redirects=False)
        r2 = client.post(f"/calls/{cid}/hide", follow_redirects=False)
    assert r2.status_code == 303
    with pg_session_factory() as s:
        rows = (
            s.execute(
                select(UserCallDismissal).where(
                    UserCallDismissal.user_id == uid, UserCallDismissal.call_id == cid
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


def test_hide_404_for_unknown_call(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _logged_in(app, pg_session_factory)
    with client:
        r = client.post("/calls/999999/hide", follow_redirects=False)
    assert r.status_code == 404


def test_hide_requires_auth(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(f"/calls/{cid}/hide", follow_redirects=False)
    assert r.status_code == 401


def test_dashboard_omits_dismissed_calls(monkeypatch, pg_url, pg_session_factory):
    _seed_call(pg_session_factory, title="VisibleCall")
    hidden = _seed_call(pg_session_factory, title="HiddenCall")
    app = _setup(monkeypatch, pg_url)
    client, _ = _logged_in(app, pg_session_factory)
    with client:
        client.post(f"/calls/{hidden}/hide")
        r = client.get("/")
    assert r.status_code == 200
    assert "VisibleCall" in r.text
    assert "HiddenCall" not in r.text
