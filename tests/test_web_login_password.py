from fastapi.testclient import TestClient

import call_manager.web.routes.auth as authmod
from call_manager.auth.passwords import hash_password
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


def _user(sf, *, email, status, password=None):
    with sf() as s:
        u = User(
            email=email, status=status, password_hash=hash_password(password) if password else None
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_active_user_logs_in(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        r = c.post(
            "/login",
            data={"email": "a@ki.se", "password": "correct-horse-1"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "session=" in "".join(r.headers.get_list("set-cookie"))


def test_failure_responses_are_identical(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    _user(app.state.session_factory, email="g@ki.se", status="active", password=None)  # oauth-only
    with TestClient(app) as c:
        wrong = c.post("/login", data={"email": "a@ki.se", "password": "nope-long-enough"})
        unknown = c.post("/login", data={"email": "ghost@ki.se", "password": "nope-long-enough"})
        oauth = c.post("/login", data={"email": "g@ki.se", "password": "nope-long-enough"})
    assert wrong.status_code == unknown.status_code == oauth.status_code == 200
    assert "invalid email or password" in wrong.text.lower()
    assert wrong.text == unknown.text == oauth.text  # no enumeration by body
    for r in (wrong, unknown, oauth):
        assert "set-cookie" not in {k.lower() for k in r.headers}


def test_dummy_hash_invoked_for_unknown_email(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    seen = []
    real = authmod.verify_password
    monkeypatch.setattr(authmod, "verify_password", lambda h, p: (seen.append(h), real(h, p))[1])
    with TestClient(app) as c:
        c.post("/login", data={"email": "ghost@ki.se", "password": "whatever-long"})
    assert seen == [None]  # verified against dummy (stored hash is None)


def test_pending_and_disabled_branch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="p@ki.se", status="pending", password="correct-horse-1")
    _user(app.state.session_factory, email="d@ki.se", status="disabled", password="correct-horse-1")
    with TestClient(app) as c:
        rp = c.post(
            "/login",
            data={"email": "p@ki.se", "password": "correct-horse-1"},
            follow_redirects=False,
        )
        rd = c.post(
            "/login",
            data={"email": "d@ki.se", "password": "correct-horse-1"},
            follow_redirects=False,
        )
    assert rp.headers["location"] == "/auth/pending"
    assert rd.headers["location"] == "/auth/no-access"


def test_repeated_bad_logins_rate_limited(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    _user(app.state.session_factory, email="a@ki.se", status="active", password="correct-horse-1")
    with TestClient(app) as c:
        for _ in range(5):  # per-email limiter is 5/15min
            c.post("/login", data={"email": "a@ki.se", "password": "bad-but-long"})
        # 6th attempt with the CORRECT password is now rate-limited -> no session
        r = c.post(
            "/login",
            data={"email": "a@ki.se", "password": "correct-horse-1"},
            follow_redirects=False,
        )
    assert r.status_code == 200 and "set-cookie" not in {k.lower() for k in r.headers}


def test_magic_link_routes_gone(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        assert c.get("/login/verify?token=x", follow_redirects=False).status_code == 404
