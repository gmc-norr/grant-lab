"""Route-wiring for session-revocation: which invalidation routes call
signal_user_revocation, and with which close code. The teardown primitive itself
is unit-tested in tests/proposal_assist/collab/test_collab_revoke_user.py; here we
patch signal_user_revocation in each route module's namespace and assert the wiring
(target user_id + close code), so these tests stay fast and DB-only."""

from fastapi.testclient import TestClient

from grant_lab.auth.passwords import hash_password
from grant_lab.db.models import User
from grant_lab.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def _user(sf, *, email, status="active", is_admin=False, password=None, oidc_sub=None):
    with sf() as s:
        u = User(
            email=email,
            status=status,
            is_admin=is_admin,
            password_hash=hash_password(password) if password else None,
            **(
                {"oidc_iss": "https://accounts.google.com", "oidc_sub": oidc_sub}
                if oidc_sub
                else {}
            ),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _client(app, uid):
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c


def _capture(monkeypatch, module_path):
    """Patch signal_user_revocation in a route module's namespace and record its
    (user_id, close_code) calls. raising=False keeps the pre-implementation red a
    clean assertion failure rather than an AttributeError at patch time."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        f"{module_path}.signal_user_revocation",
        lambda request, user_id, *, close_code=1008: calls.append((user_id, close_code)),
        raising=False,
    )
    return calls


def test_logout_signals_revocation_1008(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, email="lo@umu.se", password="correct-horse-1")
    calls = _capture(monkeypatch, "grant_lab.web.api.auth")
    with _client(app, uid) as c:
        r = c.post("/api/auth/logout")
    assert r.status_code == 200
    assert calls == [(uid, 1008)]  # terminal close


def test_logout_without_cookie_does_not_signal(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    calls = _capture(monkeypatch, "grant_lab.web.api.auth")
    with TestClient(app) as c:
        r = c.post("/api/auth/logout")
    assert r.status_code == 200
    assert calls == []  # no user resolved -> nothing to revoke


def test_change_password_signals_revocation_1012(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    uid = _user(app.state.session_factory, email="cp@umu.se", password="oldpassword12")
    calls = _capture(monkeypatch, "grant_lab.web.api.me")
    with _client(app, uid) as c:
        r = c.post(
            "/api/me/password",
            json={"current_password": "oldpassword12", "new_password": "newlongpassword"},
        )
    assert r.status_code == 200
    assert calls == [(uid, 1012)]  # reconnectable: the acting browser re-mints + reconnects


def test_admin_disable_signals_1008(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    aid = _user(app.state.session_factory, email="adm@umu.se", is_admin=True)
    tid = _user(app.state.session_factory, email="tgt@umu.se")
    calls = _capture(monkeypatch, "grant_lab.web.api.admin_users")
    with _client(app, aid) as c:
        r = c.post(f"/api/admin/users/{tid}/disable")
    assert r.status_code == 200
    assert calls == [(tid, 1008)]


def test_admin_reset_password_signals_1008(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    aid = _user(app.state.session_factory, email="adm2@umu.se", is_admin=True)
    tid = _user(app.state.session_factory, email="tgt2@umu.se")
    calls = _capture(monkeypatch, "grant_lab.web.api.admin_users")
    with _client(app, aid) as c:
        r = c.post(
            f"/api/admin/users/{tid}/reset-password",
            json={"new_password": "newlongpassword"},
        )
    assert r.status_code == 200
    assert calls == [(tid, 1008)]


def test_admin_approve_with_password_signals_1008(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    aid = _user(app.state.session_factory, email="adm3@umu.se", is_admin=True)
    tid = _user(app.state.session_factory, email="pend@umu.se", status="pending")
    calls = _capture(monkeypatch, "grant_lab.web.api.admin_users")
    with _client(app, aid) as c:
        r = c.post(
            f"/api/admin/users/{tid}/approve",
            json={"role": "member", "initial_password": "newlongpassword"},
        )
    assert r.status_code == 200
    assert calls == [(tid, 1008)]  # a credential was issued -> revoke prior sessions


def test_admin_approve_oidc_without_password_does_not_signal(
    monkeypatch, pg_url, pg_session_factory
):
    app = _app(monkeypatch, pg_url)
    aid = _user(app.state.session_factory, email="adm4@umu.se", is_admin=True)
    tid = _user(
        app.state.session_factory, email="goog@umu.se", status="pending", oidc_sub="sub-xyz"
    )
    calls = _capture(monkeypatch, "grant_lab.web.api.admin_users")
    with _client(app, aid) as c:
        r = c.post(f"/api/admin/users/{tid}/approve", json={"role": "member"})
    assert r.status_code == 200
    assert calls == []  # no epoch bump (no credential issued) -> no revoke


def test_admin_enable_and_change_role_do_not_signal(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    aid = _user(app.state.session_factory, email="adm5@umu.se", is_admin=True)
    disabled = _user(app.state.session_factory, email="dis@umu.se", status="disabled")
    active = _user(app.state.session_factory, email="act@umu.se")
    calls = _capture(monkeypatch, "grant_lab.web.api.admin_users")
    with _client(app, aid) as c:
        assert c.post(f"/api/admin/users/{disabled}/enable").status_code == 200
        assert c.post(f"/api/admin/users/{active}/role", json={"role": "admin"}).status_code == 200
    assert calls == []  # neither bumps session_epoch -> neither revokes
