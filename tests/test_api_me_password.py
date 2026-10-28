from fastapi.testclient import TestClient

from grant_lab.auth.passwords import hash_password, verify_password
from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _login(app, pg_session_factory, *, password_hash=None, oidc=False, email="u@umu.se"):
    with pg_session_factory() as s:
        u = User(
            status="active",
            email=email,
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


def test_repeated_wrong_current_password_locks_the_change_out(
    monkeypatch, pg_url, pg_session_factory
):
    """A session cookie must not be a password brute-force oracle.

    `POST /api/me/password` had no attempt limit while `/auth/login` did
    (review 2026-10-26 A7). So someone holding a stolen session could grind the
    account password — which outlives a logout — and each try cost an argon2
    verify on the threadpool. The login `email_limiter` (5 / 15 min) is reused,
    keyed on the USER ID, so the refusal is per-account and cannot be used to
    lock a victim out by guessing their address.

    The 41st request carries the CORRECT current password: before the fix it
    answered 200 and changed the credential.
    """
    app = _setup(monkeypatch, pg_url)
    client, uid = _login(app, pg_session_factory, password_hash=hash_password("rightpassword"))
    with client:
        for _ in range(40):
            client.post(
                "/api/me/password",
                json={"current_password": "wrongpassword", "new_password": "newlongpassword"},
            )
        r = client.post(
            "/api/me/password",
            json={"current_password": "rightpassword", "new_password": "newlongpassword"},
        )
    assert r.status_code == 429, (
        f"40 wrong attempts must lock the change out; got {r.status_code}: {r.text}"
    )
    assert r.json()["error"]["code"] == "too_many_requests"
    with pg_session_factory() as s:
        u = s.get(User, uid)
        ok, _ = verify_password(u.password_hash, "rightpassword")
        assert ok, "the refused attempt must not have changed the credential"
        assert u.session_epoch == 0, "a refused attempt must not revoke sessions"


def test_a_user_with_no_failed_attempts_is_not_locked_out(monkeypatch, pg_url, pg_session_factory):
    """POSITIVE CONTROL for the limiter above, two ways.

    A fix that simply refuses every password change would pass the lockout test.
    So: (1) the untouched user B changes their password in the SAME process
    whose limiter has already refused user A, which also proves the key is the
    user id and not a global counter; and (2) B's credential really changed.
    """
    app = _setup(monkeypatch, pg_url)
    client, uid_a = _login(app, pg_session_factory, password_hash=hash_password("rightpassword"))
    with pg_session_factory() as s:
        b = User(
            status="active",
            email="b@umu.se",
            password_hash=hash_password("bbbpassword12"),
            session_epoch=0,
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        uid_b = b.id
    with client:
        for _ in range(10):
            client.post(
                "/api/me/password",
                json={"current_password": "wrongpassword", "new_password": "newlongpassword"},
            )
        locked = client.post(
            "/api/me/password",
            json={"current_password": "rightpassword", "new_password": "newlongpassword"},
        )
        client.cookies.set("session", app.state.session_signer.make(user_id=uid_b, session_epoch=0))
        allowed = client.post(
            "/api/me/password",
            json={"current_password": "bbbpassword12", "new_password": "newlongpassword"},
        )
    assert locked.status_code == 429, f"precondition: A is locked out, got {locked.status_code}"
    assert allowed.status_code == 200, (
        f"B made no failed attempt and must still be able to change their password; "
        f"got {allowed.status_code}: {allowed.text}"
    )
    with pg_session_factory() as s:
        ok, _ = verify_password(s.get(User, uid_b).password_hash, "newlongpassword")
        assert ok, "B's credential must actually have changed"
        a_ok, _ = verify_password(s.get(User, uid_a).password_hash, "rightpassword")
        assert a_ok, "A's credential must be untouched"
