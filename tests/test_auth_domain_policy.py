"""ALLOWED_EMAIL_DOMAINS gates sign-in AND sessions, for every non-admin account.

Security review 2026-10-31 S3: registration and Google sign-in checked the
allowlist, but password login did not. So a member admitted under a domain
that was later REMOVED from the list still signed in, and kept any session.

Policy (decided 2026-10-31): a non-admin whose email domain is not allowed
cannot sign in with a password and loses an existing session at its next
request. Administrators are exempt -- the live install has active admins on
personal and partner addresses, and a strict check would lock them out.
Google sign-in stays strict for everyone, because it is also the path that
CREATES accounts.
"""

import pytest
from fastapi.testclient import TestClient

from grant_lab.auth.passwords import hash_password
from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import User

PW = "a-long-password-1"


def _setup(make_app, monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return make_app()


def _user(sf, email, *, is_admin=False):
    with sf() as s:
        u = User(
            email=email,
            status="active",
            is_admin=is_admin,
            session_epoch=0,
            password_hash=hash_password(PW),
        )
        s.add(u)
        s.commit()
        return u.id


def _login(app, email):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": PW})
    return r, c


def _session(app, uid):
    signer: SessionSigner = app.state.session_signer
    c = TestClient(app)
    c.cookies.set("session", signer.make(user_id=uid, session_epoch=0))
    return c


# --- password login ----------------------------------------------------------


def test_a_member_outside_the_allowlist_cannot_sign_in_with_a_password(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    _user(pg_session_factory, "former-partner@ki.se")

    r, c = _login(app, "former-partner@ki.se")

    assert r.status_code == 200
    assert r.json() == {"redirect": "/auth/no-access"}
    assert "session" not in r.cookies
    assert c.get("/api/me").status_code == 401


@pytest.mark.parametrize(
    ("email", "is_admin"),
    [
        pytest.param("member@umu.se", False, id="member-inside"),
        pytest.param("admin@gmail.com", True, id="admin-outside"),
    ],
)
def test_allowed_accounts_still_sign_in(
    email, is_admin, make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    _user(pg_session_factory, email, is_admin=is_admin)

    r, c = _login(app, email)

    assert r.status_code == 200
    assert r.json() == {"redirect": "/"}
    assert c.get("/api/me").status_code == 200


# --- existing sessions -------------------------------------------------------


def test_a_members_session_ends_once_their_domain_is_not_allowed(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    uid = _user(pg_session_factory, "former-partner@ki.se")
    c = _session(app, uid)

    r = c.get("/api/me")

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_inactive"
    assert r.json()["error"]["detail"] == {"redirect": "/auth/no-access"}


def test_an_admins_session_outside_the_allowlist_keeps_working(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    uid = _user(pg_session_factory, "admin@gmail.com", is_admin=True)
    c = _session(app, uid)

    assert c.get("/api/me").status_code == 200
    assert c.get("/api/admin/users").status_code == 200


def test_demoting_an_outside_admin_ends_their_access(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    """The exemption follows the role, not the account's history."""
    app = _setup(make_app, monkeypatch, pg_url)
    uid = _user(pg_session_factory, "admin@gmail.com", is_admin=True)
    c = _session(app, uid)
    assert c.get("/api/me").status_code == 200

    with pg_session_factory() as s:
        s.get(User, uid).is_admin = False
        s.commit()

    assert c.get("/api/me").status_code == 403
