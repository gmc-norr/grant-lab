from fastapi.testclient import TestClient

import call_manager.web.api.auth as apiauth
from call_manager.auth.passwords import hash_password
from call_manager.db.models import User
from call_manager.main import create_app


def _app(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
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
    assert body["allowed_domains"] == ["ki.se"]


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
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": "a@ki.se", "password": "correct-horse-1"})
    assert r.status_code == 200 and r.json() == {"redirect": "/"}
    assert "session=" in "".join(r.headers.get_list("set-cookie"))


def test_login_active_honors_safe_next(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        ok = c.post(
            "/api/auth/login",
            json={"email": "a@ki.se", "password": "correct-horse-1", "next": "/calls/5"},
        )
        bad = c.post(
            "/api/auth/login",
            json={"email": "a@ki.se", "password": "correct-horse-1", "next": "//evil.example"},
        )
    assert ok.json()["redirect"] == "/calls/5"
    assert bad.json()["redirect"] == "/"  # open-redirect rejected -> "/"


def test_login_pending_and_no_access_no_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="p@ki.se", status="pending", password="correct-horse-1")
    _user(app.state.session_factory, email="d@ki.se", status="disabled", password="correct-horse-1")
    with TestClient(app) as c:
        rp = c.post("/api/auth/login", json={"email": "p@ki.se", "password": "correct-horse-1"})
        rd = c.post("/api/auth/login", json={"email": "d@ki.se", "password": "correct-horse-1"})
    assert rp.json() == {"redirect": "/auth/pending"}
    assert rd.json() == {"redirect": "/auth/no-access"}
    for r in (rp, rd):
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_failures_identical_400_no_cookie(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    _user(app.state.session_factory, email="g@ki.se", status="active", password=None)  # oauth-only
    with TestClient(app) as c:
        wrong = c.post("/api/auth/login", json={"email": "a@ki.se", "password": "nope-long-enough"})
        unknown = c.post(
            "/api/auth/login", json={"email": "ghost@ki.se", "password": "nope-long-enough"}
        )
        oauth = c.post("/api/auth/login", json={"email": "g@ki.se", "password": "nope-long-enough"})
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
        c.post("/api/auth/login", json={"email": "ghost@ki.se", "password": "whatever-long"})
    assert seen == [None]  # verified against dummy (stored hash None)


def test_login_rate_limited_folds_into_400(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    from call_manager.auth.rate_limit import SlidingWindowLimiter

    app.state.email_limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    with TestClient(app) as c:
        for _ in range(2):
            c.post("/api/auth/login", json={"email": "a@ki.se", "password": "bad-but-long"})
        # now rate-limited even WITH the correct password
        r = c.post("/api/auth/login", json={"email": "a@ki.se", "password": "correct-horse-1"})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "Invalid email or password."  # non-disclosure
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_login_oversized_email_400_no_limiter_growth(monkeypatch, pg_url, pg_session_factory):
    app = _app(monkeypatch, pg_url)
    huge = "a" * 400 + "@ki.se"
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": huge, "password": "x" * 16})
    assert r.status_code == 400
    assert huge not in app.state.email_limiter._events
