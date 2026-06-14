# tests/test_web_admin_users.py
from fastapi.testclient import TestClient

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


def _admin(app, pg_session_factory, email="admin@ki.se"):
    with pg_session_factory() as s:
        a = User(email=email, status="active", is_admin=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        aid = a.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=aid))
    return c, aid


def _pending(pg_session_factory, email, sub):
    with pg_session_factory() as s:
        p = User(email=email, status="pending", oidc_iss="i", oidc_sub=sub)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


def test_pending_queue_lists_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _pending(pg_session_factory, "p@gmail.com", "s0")
    r = c.get("/admin/users/pending")
    assert r.status_code == 200 and "p@gmail.com" in r.text


def test_approve_member(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _pending(pg_session_factory, "p1@gmail.com", "s1")
    c.post(f"/admin/users/{pid}/approve", data={"role": "member"}, follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and u.is_admin is False and u.approved_at is not None


def test_approve_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _pending(pg_session_factory, "p2@gmail.com", "s2")
    c.post(f"/admin/users/{pid}/approve", data={"role": "admin"}, follow_redirects=False)
    with pg_session_factory() as s:
        assert s.get(User, pid).is_admin is True


def test_approve_rejects_non_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    # the admin is active, not pending -> approve must refuse (can't demote via approve)
    r = c.post(f"/admin/users/{aid}/approve", data={"role": "member"}, follow_redirects=False)
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).is_admin is True


def test_deny_only_pending(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _pending(pg_session_factory, "p3@gmail.com", "s3")
    c.post(f"/admin/users/{pid}/deny", follow_redirects=False)
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "disabled"


def test_enable_only_disabled(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _pending(pg_session_factory, "p4@gmail.com", "s4")  # pending, not disabled
    r = c.post(f"/admin/users/{pid}/enable", follow_redirects=False)
    assert r.status_code == 400  # can't activate a pending user without approval
    with pg_session_factory() as s:
        assert s.get(User, pid).status == "pending"


def test_disable_bumps_epoch(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        s.add(User(email="admin2@ki.se", status="active", is_admin=True))  # keep >1 admin
        m = User(email="m@ki.se", status="active")
        s.add(m)
        s.commit()
        s.refresh(m)
        mid, before = m.id, m.session_epoch
    c.post(f"/admin/users/{mid}/disable", follow_redirects=False)
    with pg_session_factory() as s:
        u = s.get(User, mid)
        assert u.status == "disabled" and u.session_epoch == before + 1


def test_change_role_demote_guarded(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    # only one active admin -> demoting them must fail
    r = c.post(f"/admin/users/{aid}/role", data={"role": "member"}, follow_redirects=False)
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).is_admin is True


def test_cannot_disable_last_active_admin(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    r = c.post(f"/admin/users/{aid}/disable", follow_redirects=False)
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).status == "active"


def test_non_admin_blocked(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="member@ki.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        assert c.get("/admin/users/pending").status_code == 403
