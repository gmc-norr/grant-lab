from fastapi.testclient import TestClient
from sqlalchemy import select

import call_manager.web.routes.register as reg
from call_manager.db.models import User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url, "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se", "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_register_creates_pending_request_no_password_truncates_name(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post("/register", data={"email": "new@ki.se", "name": "z" * 300},
                   follow_redirects=False)
        assert r.status_code == 200
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "new@ki.se")).scalar_one()
        assert u.status == "pending" and u.password_hash is None and len(u.name) == 200


def test_duplicate_via_integrity_error_no_second_row_identical_body(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active")); s.commit()
    with TestClient(app) as c:
        fresh = c.post("/register", data={"email": "fresh@ki.se"})
        dup = c.post("/register", data={"email": "dupe@ki.se"})
        assert dup.status_code == 200
        assert dup.text == fresh.text  # identical body -> no enumeration
    with pg_session_factory() as s:
        assert len(s.execute(select(User).where(User.email == "dupe@ki.se")).scalars().all()) == 1


def test_dummy_hash_invoked_on_every_eligible_attempt(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        s.add(User(email="dupe@ki.se", status="active")); s.commit()
    calls = []
    real = reg.verify_password
    monkeypatch.setattr(reg, "verify_password", lambda h, p: (calls.append(h), real(h, p))[1])
    with TestClient(app) as c:
        c.post("/register", data={"email": "new2@ki.se"})  # fresh
        c.post("/register", data={"email": "dupe@ki.se"})  # duplicate
    assert calls == [None, None]  # dummy-hash on BOTH paths


def test_register_domain_disallowed(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with TestClient(app) as c:
        c.post("/register", data={"email": "x@evil.com"})
    with pg_session_factory() as s:
        assert s.execute(select(User).where(User.email == "x@evil.com")).scalar_one_or_none() is None
