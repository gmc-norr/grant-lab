from fastapi.testclient import TestClient

from call_manager.auth.passwords import verify_password
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _admin_client(app, sf):
    with sf() as s:
        a = User(email="admin@ki.se", status="active", is_admin=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=aid))
    return c


def test_approve_local_requires_password_and_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c = _admin_client(app, app.state.session_factory)
    with pg_session_factory() as s:
        p = User(email="local@ki.se", status="pending")  # no oidc, no password
        s.add(p)
        s.commit()
        s.refresh(p)
        pid, ep0 = p.id, p.session_epoch
    assert (
        c.post(
            f"/admin/users/{pid}/approve", data={"role": "member"}, follow_redirects=False
        ).status_code
        == 400
    )  # no password -> rejected
    c.post(
        f"/admin/users/{pid}/approve",
        data={"role": "member", "initial_password": "issued-strong-pw"},
        follow_redirects=False,
    )
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and verify_password(u.password_hash, "issued-strong-pw")
        assert u.session_epoch == ep0 + 1  # credential issued -> epoch bumped


def test_approve_oauth_pending_without_password(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c = _admin_client(app, app.state.session_factory)
    with pg_session_factory() as s:
        p = User(email="goog@ki.se", status="pending", oidc_iss="i", oidc_sub="s")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
    assert (
        c.post(
            f"/admin/users/{pid}/approve", data={"role": "member"}, follow_redirects=False
        ).status_code
        == 303
    )
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "active"


def test_admin_reset_password_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c = _admin_client(app, app.state.session_factory)
    with pg_session_factory() as s:
        u = User(email="u@ki.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid, ep0 = u.id, u.session_epoch
    c.post(
        f"/admin/users/{uid}/reset-password",
        data={"new_password": "fresh-strong-pw"},
        follow_redirects=False,
    )
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert verify_password(u.password_hash, "fresh-strong-pw") and u.session_epoch == ep0 + 1


def test_reset_rejects_short(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c = _admin_client(app, app.state.session_factory)
    with pg_session_factory() as s:
        u = User(email="u2@ki.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    assert (
        c.post(
            f"/admin/users/{uid}/reset-password",
            data={"new_password": "short"},
            follow_redirects=False,
        ).status_code
        == 400
    )
