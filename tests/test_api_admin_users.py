from datetime import UTC, datetime

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


def _admin(app, sf, email="admin@ki.se"):
    with sf() as s:
        a = User(email=email, status="active", is_admin=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=aid))
    return c, aid


def _user(sf, *, email, status="active", is_admin=False, oidc_sub=None):
    with sf() as s:
        u = User(
            email=email,
            status=status,
            is_admin=is_admin,
            **({"oidc_iss": "i", "oidc_sub": oidc_sub} if oidc_sub else {}),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def test_list_users_excludes_pending_with_pagination(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="active@ki.se", status="active")
    _user(pg_session_factory, email="disabled@ki.se", status="disabled")
    _user(pg_session_factory, email="pend@ki.se", status="pending")
    r = c.get("/api/admin/users")
    assert r.status_code == 200
    body = r.json()
    emails = {u["email"] for u in body["users"]}
    assert "active@ki.se" in emails and "disabled@ki.se" in emails
    assert "pend@ki.se" not in emails  # pending excluded
    assert body["page"] == 1 and body["per_page"] == 50
    assert body["total"] == len(emails)  # admin + active + disabled, no pending
    row = next(u for u in body["users"] if u["email"] == "active@ki.se")
    assert set(row) == {"id", "email", "name", "status", "is_admin"}


def test_pending_list_shape(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="local@ki.se", status="pending")
    _user(pg_session_factory, email="goog@ki.se", status="pending", oidc_sub="s1")
    r = c.get("/api/admin/users/pending")
    assert r.status_code == 200
    body = r.json()
    rows = {u["email"]: u for u in body["users"]}
    assert rows["local@ki.se"]["google_linked"] is False
    assert rows["goog@ki.se"]["google_linked"] is True
    assert body["page"] == 1


def test_reads_require_admin_403(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    uid = _user(pg_session_factory, email="member@ki.se", status="active")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        assert c.get("/api/admin/users").status_code == 403
        assert c.get("/api/admin/users/pending").status_code == 403
