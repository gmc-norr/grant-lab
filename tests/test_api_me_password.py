from fastapi.testclient import TestClient

from grant_lab.auth.passwords import hash_password
from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory, *, password_hash=None, oidc=False):
    with pg_session_factory() as s:
        u = User(
            status="active",
            email="u@umu.se",
            password_hash=password_hash,
            session_epoch=0,
            oidc_iss=("https://accounts.google.com" if oidc else None),
            oidc_sub=("sub-1" if oidc else None),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid, session_epoch=0))
    return client, uid


def test_password_change_remints_cookie_and_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory, password_hash=hash_password("oldpassword12"))
    with client:
        r = client.post(
            "/api/me/password",
            json={"current_password": "oldpassword12", "new_password": "newlongpassword"},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    setc = r.headers["set-cookie"].lower()
    assert "httponly" in setc
    assert "samesite=lax" in setc
    assert f"max-age={app.state.settings.session_max_age_seconds}" in setc
    assert ("secure" in setc) == app.state.settings.cookie_secure
    with pg_session_factory() as s:
        assert s.get(User, uid).session_epoch == 1


def test_password_wrong_current_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory, password_hash=hash_password("rightpassword"))
    with client:
        r = client.post(
            "/api/me/password",
            json={"current_password": "wrongpassword", "new_password": "newlongpassword"},
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_password_weak_new_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory, password_hash=hash_password("oldpassword12"))
    with client:
        r = client.post(
            "/api/me/password", json={"current_password": "oldpassword12", "new_password": "a"}
        )
    assert r.status_code == 400


def test_password_oauth_only_no_password_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client, _ = _login(app, pg_session_factory, password_hash=None, oidc=True)
    with client:
        r = client.post(
            "/api/me/password",
            json={"current_password": "anything", "new_password": "newlongpassword"},
        )
    assert r.status_code == 400
