from fastapi.testclient import TestClient

from call_manager.auth.passwords import hash_password, verify_password
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


def _client(app, sf, password=None):
    with sf() as s:
        u = User(
            email="u@ki.se",
            status="active",
            password_hash=hash_password(password) if password else None,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        uid, ep = u.id, u.session_epoch
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid, session_epoch=ep))
    return c, uid


def test_change_password_requires_current_and_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _client(app, app.state.session_factory, password="old-strong-pw")
    with pg_session_factory() as s:
        ep0 = s.get(User, uid).session_epoch
    r = c.post(
        "/me/password",
        data={"current_password": "old-strong-pw", "new_password": "new-strong-pw"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "session=" in "".join(r.headers.get_list("set-cookie"))  # re-minted
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert verify_password(u.password_hash, "new-strong-pw") and u.session_epoch == ep0 + 1


def test_change_password_wrong_current_rejected(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _client(app, app.state.session_factory, password="old-strong-pw")
    assert (
        c.post(
            "/me/password",
            data={"current_password": "WRONG", "new_password": "new-strong-pw"},
            follow_redirects=False,
        ).status_code
        == 400
    )


def test_oauth_only_user_cannot_set_first_password(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, uid = _client(app, app.state.session_factory, password=None)  # no hash
    assert (
        c.post(
            "/me/password",
            data={"current_password": "", "new_password": "new-strong-pw"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    with pg_session_factory() as s:
        assert s.get(User, uid).password_hash is None
