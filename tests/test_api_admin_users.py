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


def test_approve_member_and_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="p1@ki.se", status="pending", oidc_sub="s1")
    assert c.post(f"/api/admin/users/{pid}/approve", json={"role": "member"}).status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and u.is_admin is False and u.approved_at is not None
        assert u.approved_by_user_id == aid
    pid2 = _user(pg_session_factory, email="p2@ki.se", status="pending", oidc_sub="s2")
    c.post(f"/api/admin/users/{pid2}/approve", json={"role": "admin"})
    with pg_session_factory() as s:
        assert s.get(User, pid2).is_admin is True


def test_approve_defaults_role_to_member(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="p3@ki.se", status="pending", oidc_sub="s3")
    assert c.post(f"/api/admin/users/{pid}/approve", json={}).status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, pid).is_admin is False


def test_approve_rejects_non_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)  # admin is active, not pending
    r = c.post(f"/api/admin/users/{aid}/approve", json={"role": "member"})
    assert r.status_code == 400


def test_approve_invalid_role_on_nonexistent_is_400_not_404(monkeypatch, pg_url, pg_session_factory):
    # ordering lock: role is validated BEFORE the user lookup.
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/users/999999/approve", json={"role": "galaxy"})
    assert r.status_code == 400


def test_approve_404_for_unknown_user(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/users/999999/approve", json={"role": "member"})
    assert r.status_code == 404


def test_approve_local_requires_password_then_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="local@ki.se", status="pending")  # no oidc, no password
        s.add(p)
        s.commit()
        s.refresh(p)
        pid, ep0 = p.id, p.session_epoch
    assert c.post(f"/api/admin/users/{pid}/approve", json={"role": "member"}).status_code == 400
    c.post(
        f"/api/admin/users/{pid}/approve",
        json={"role": "member", "initial_password": "issued-strong-pw"},
    )
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and verify_password(u.password_hash, "issued-strong-pw")
        assert u.session_epoch == ep0 + 1


def test_approve_oauth_without_password_does_not_bump_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="g@ki.se", status="pending", oidc_sub="s9")
    with pg_session_factory() as s:
        ep0 = s.get(User, pid).session_epoch
    assert c.post(f"/api/admin/users/{pid}/approve", json={"role": "member"}).status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active"
        assert u.session_epoch == ep0  # passwordless OAuth approve does NOT bump epoch


def test_approve_weak_password_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="local2@ki.se", status="pending")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
    r = c.post(f"/api/admin/users/{pid}/approve", json={"role": "member", "initial_password": "short"})
    assert r.status_code == 400


def test_deny_only_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="d@ki.se", status="pending", oidc_sub="sd")
    with pg_session_factory() as s:
        ep0 = s.get(User, pid).session_epoch
    assert c.post(f"/api/admin/users/{pid}/deny").status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "disabled"
        assert u.session_epoch == ep0  # deny does NOT bump epoch
    # denying a non-pending user -> 400
    uid = _user(pg_session_factory, email="active2@ki.se", status="active")
    assert c.post(f"/api/admin/users/{uid}/deny").status_code == 400


def test_deny_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/deny").status_code == 404


def test_disable_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="admin2@ki.se", status="active", is_admin=True)  # keep >1 admin
    with pg_session_factory() as s:
        m = User(email="m@ki.se", status="active")
        s.add(m)
        s.commit()
        s.refresh(m)
        mid, before = m.id, m.session_epoch
    assert c.post(f"/api/admin/users/{mid}/disable").status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, mid)
        assert u.status == "disabled" and u.session_epoch == before + 1


def test_disable_only_active_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="dis@ki.se", status="disabled")
    assert c.post(f"/api/admin/users/{did}/disable").status_code == 400


def test_cannot_disable_last_active_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    # aid is the only active admin; try to disable themselves
    # This now hits the self-disable guard (403) before the last-admin guard (400)
    assert c.post(f"/api/admin/users/{aid}/disable").status_code == 403
    with pg_session_factory() as s:
        assert s.get(User, aid).status == "active"


def test_disable_404(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/disable").status_code == 404


def test_change_role_to_admin_and_member(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)  # keeps an admin around for demote tests
    uid = _user(pg_session_factory, email="r@ki.se", status="active", is_admin=False)
    with pg_session_factory() as s:
        ep0 = s.get(User, uid).session_epoch
    assert c.post(f"/api/admin/users/{uid}/role", json={"role": "admin"}).status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, uid).is_admin is True
    assert c.post(f"/api/admin/users/{uid}/role", json={"role": "member"}).status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.is_admin is False
        assert u.session_epoch == ep0  # role changes do NOT bump epoch


def test_change_role_demote_last_admin_guarded(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)  # only one active admin
    r = c.post(f"/api/admin/users/{aid}/role", json={"role": "member"})
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).is_admin is True


def test_change_role_invalid_role_on_nonexistent_is_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/role", json={"role": "galaxy"}).status_code == 400


def test_change_role_not_active_400(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="rd@ki.se", status="disabled")
    assert c.post(f"/api/admin/users/{did}/role", json={"role": "admin"}).status_code == 400


def test_cannot_disable_self(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="admin2@ki.se", status="active", is_admin=True)  # keep >1 admin
    assert c.post(f"/api/admin/users/{aid}/disable").status_code == 403
    with pg_session_factory() as s:
        assert s.get(User, aid).status == "active"
