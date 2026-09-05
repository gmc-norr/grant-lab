from fastapi.testclient import TestClient

import grant_lab.web.api.auth as apiauth
from grant_lab.auth.passwords import hash_password
from grant_lab.db.models import User
from grant_lab.main import create_app


def _app(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        **extra,
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_config_reports_domains_and_google_disabled(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["google_oidc_enabled"] is False
    assert body["allowed_domains"] == ["umu.se"]


def test_config_reports_google_enabled(monkeypatch, pg_url, pg_session_factory):
    app = _app(
        monkeypatch,
        pg_url,
        GOOGLE_OIDC_ENABLED="true",
        GOOGLE_CLIENT_ID="cid",
        GOOGLE_CLIENT_SECRET="sec",
    )
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert r.json()["google_oidc_enabled"] is True


def test_config_is_public_no_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.get("/api/auth/config")
    assert "set-cookie" not in {k.lower() for k in r.headers}


def _user(sf, *, email, status, password=None):
    with sf() as s:
        u = User(
            email=email,
            status=status,
            password_hash=hash_password(password) if password else None,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_login_active_sets_cookie_redirect_root(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@umu.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": "a@umu.se", "password": "correct-horse-1"})
    assert r.status_code == 200 and r.json() == {"redirect": "/"}
    assert "session=" in "".join(r.headers.get_list("set-cookie"))


def test_login_active_honors_safe_next(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@umu.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        ok = c.post(
            "/api/auth/login",
            json={"email": "a@umu.se", "password": "correct-horse-1", "next": "/calls/5"},
        )
        bad = c.post(
            "/api/auth/login",
            json={"email": "a@umu.se", "password": "correct-horse-1", "next": "//evil.example"},
        )
    assert ok.json()["redirect"] == "/calls/5"
    assert bad.json()["redirect"] == "/"  # open-redirect rejected -> "/"


def test_login_pending_and_no_access_no_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="p@umu.se", status="pending", password="correct-horse-1")
    _user(
        app.state.session_factory, email="d@umu.se", status="disabled", password="correct-horse-1"
    )
    with TestClient(app) as c:
        rp = c.post("/api/auth/login", json={"email": "p@umu.se", "password": "correct-horse-1"})
        rd = c.post("/api/auth/login", json={"email": "d@umu.se", "password": "correct-horse-1"})
    assert rp.json() == {"redirect": "/auth/pending"}
    assert rd.json() == {"redirect": "/auth/no-access"}
    for r in (rp, rd):
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_failures_identical_400_no_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@umu.se", status="active", password="correct-horse-1")
    _user(app.state.session_factory, email="g@umu.se", status="active", password=None)  # oauth-only
    with TestClient(app) as c:
        wrong = c.post(
            "/api/auth/login", json={"email": "a@umu.se", "password": "nope-long-enough"}
        )
        unknown = c.post(
            "/api/auth/login", json={"email": "ghost@umu.se", "password": "nope-long-enough"}
        )
        oauth = c.post(
            "/api/auth/login", json={"email": "g@umu.se", "password": "nope-long-enough"}
        )
    assert wrong.status_code == unknown.status_code == oauth.status_code == 400
    assert wrong.json() == unknown.json() == oauth.json()  # byte-identical -> no enumeration
    assert wrong.json()["error"]["message"] == "Invalid email or password."
    for r in (wrong, unknown, oauth):
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_dummy_hash_invoked_for_unknown_email(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    seen = []
    real = apiauth.verify_password
    monkeypatch.setattr(apiauth, "verify_password", lambda h, p: (seen.append(h), real(h, p))[1])
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"email": "ghost@umu.se", "password": "whatever-long"})
    assert seen == [None]  # verified against dummy (stored hash None)


def test_login_rate_limited_folds_into_400(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@umu.se", status="active", password="correct-horse-1")
    from grant_lab.auth.rate_limit import SlidingWindowLimiter

    app.state.email_limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    with TestClient(app) as c:
        for _ in range(2):
            c.post("/api/auth/login", json={"email": "a@umu.se", "password": "bad-but-long"})
        # now rate-limited even WITH the correct password
        r = c.post("/api/auth/login", json={"email": "a@umu.se", "password": "correct-horse-1"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "Invalid email or password."  # non-disclosure
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_oversized_email_400_no_limiter_growth(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    huge = "a" * 400 + "@umu.se"
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": huge, "password": "x" * 16})
    assert r.status_code == 400
    assert huge not in app.state.email_limiter._events


def test_register_creates_pending_truncates_name(monkeypatch, pg_url, pg_session_factory):
    from sqlalchemy import select as _select

    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/api/auth/register", json={"email": "new@umu.se", "name": "z" * 300})
    assert r.status_code == 200 and r.json() == {"ok": True}
    with pg_session_factory() as s:
        u = s.execute(_select(User).where(User.email == "new@umu.se")).scalar_one()
        assert u.status == "pending" and u.password_hash is None and len(u.name) == 200


def test_register_duplicate_silent_identical_no_second_row(monkeypatch, pg_url, pg_session_factory):
    from sqlalchemy import select as _select

    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@umu.se", status="active"))
        s.commit()
    with TestClient(app) as c:
        fresh = c.post("/api/auth/register", json={"email": "fresh@umu.se"})
        dup = c.post("/api/auth/register", json={"email": "dupe@umu.se"})
    assert fresh.status_code == dup.status_code == 200
    assert fresh.json() == dup.json() == {"ok": True}
    with pg_session_factory() as s:
        assert len(s.execute(_select(User).where(User.email == "dupe@umu.se")).scalars().all()) == 1


def test_register_dummy_hash_on_every_eligible_attempt(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@umu.se", status="active"))
        s.commit()
    calls = []
    real = apiauth.verify_password
    monkeypatch.setattr(apiauth, "verify_password", lambda h, p: (calls.append(h), real(h, p))[1])
    with TestClient(app) as c:
        c.post("/api/auth/register", json={"email": "new2@umu.se"})  # fresh
        c.post("/api/auth/register", json={"email": "dupe@umu.se"})  # duplicate
    assert calls == [None, None]  # dummy-hash on BOTH paths (timing parity)


def test_register_domain_disallowed_no_row(monkeypatch, pg_url, pg_session_factory):
    from sqlalchemy import select as _select

    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/api/auth/register", json={"email": "x@evil.com"})
    assert r.status_code == 200 and r.json() == {"ok": True}  # anti-enumeration
    with pg_session_factory() as s:
        assert (
            s.execute(_select(User).where(User.email == "x@evil.com")).scalar_one_or_none() is None
        )


def test_register_rate_limited_silent_no_row(monkeypatch, pg_url, pg_session_factory):
    """Rate-limited register stays anti-enumeration: always 200 {ok: true}, NO
    row created. (Closes the Codex-flagged register rate-limit gap.)"""
    from sqlalchemy import select as _select

    app = _app(monkeypatch, pg_url)
    from grant_lab.auth.rate_limit import SlidingWindowLimiter

    app.state.ip_limiter = SlidingWindowLimiter(max_requests=0, window_seconds=60)
    with TestClient(app) as c:
        r = c.post("/api/auth/register", json={"email": "limited@umu.se"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    with pg_session_factory() as s:
        assert (
            s.execute(_select(User).where(User.email == "limited@umu.se")).scalar_one_or_none()
            is None
        )


def test_logout_bumps_epoch_and_clears_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    uid = _user(
        app.state.session_factory, email="a@umu.se", status="active", password="correct-horse-1"
    )
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid, session_epoch=0))
        r = c.post("/api/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
    set_cookie = " ".join(r.headers.get_list("set-cookie"))
    assert "session=" in set_cookie and (
        "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()
    )
    with pg_session_factory() as s:
        assert s.get(User, uid).session_epoch == 1  # revokes all outstanding cookies


def test_logout_without_cookie_is_noop_200(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/api/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
