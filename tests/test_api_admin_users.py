from fastapi.testclient import TestClient

from grant_lab.auth.passwords import verify_password
from grant_lab.db.models import User


def _setup(make_app, monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = make_app()
    return app


def _admin(app, sf, email="admin@umu.se"):
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


def test_list_users_excludes_pending_with_pagination(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="active@umu.se", status="active")
    _user(pg_session_factory, email="disabled@umu.se", status="disabled")
    _user(pg_session_factory, email="pend@umu.se", status="pending")
    r = c.get("/api/admin/users")
    assert r.status_code == 200
    body = r.json()
    emails = {u["email"] for u in body["users"]}
    assert "active@umu.se" in emails and "disabled@umu.se" in emails
    assert "pend@umu.se" not in emails  # pending excluded
    assert body["page"] == 1 and body["per_page"] == 50
    assert body["total"] == len(emails)  # admin + active + disabled, no pending
    row = next(u for u in body["users"] if u["email"] == "active@umu.se")
    assert set(row) == {"id", "email", "name", "status", "is_admin"}


def test_pending_list_shape(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="local@umu.se", status="pending")
    _user(pg_session_factory, email="goog@umu.se", status="pending", oidc_sub="s1")
    r = c.get("/api/admin/users/pending")
    assert r.status_code == 200
    body = r.json()
    rows = {u["email"]: u for u in body["users"]}
    assert rows["local@umu.se"]["google_linked"] is False
    assert rows["goog@umu.se"]["google_linked"] is True
    assert body["page"] == 1


def test_pending_list_reports_page_size_and_pending_total(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(pg_session_factory, email="p1@umu.se", status="pending")
    _user(pg_session_factory, email="p2@umu.se", status="pending")
    _user(pg_session_factory, email="active@umu.se", status="active")
    body = c.get("/api/admin/users/pending").json()
    assert body["per_page"] == 50
    assert body["total"] == 2  # pending only: not the admin, not the active user


def test_reads_require_admin_403(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    uid = _user(pg_session_factory, email="member@umu.se", status="active")
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        assert c.get("/api/admin/users").status_code == 403
        assert c.get("/api/admin/users/pending").status_code == 403


def test_approve_member_and_admin(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="p1@umu.se", status="pending", oidc_sub="s1")
    assert c.post(f"/api/admin/users/{pid}/approve", json={"role": "member"}).status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active" and u.is_admin is False and u.approved_at is not None
        assert u.approved_by_user_id == aid
    pid2 = _user(pg_session_factory, email="p2@umu.se", status="pending", oidc_sub="s2")
    c.post(f"/api/admin/users/{pid2}/approve", json={"role": "admin"})
    with pg_session_factory() as s:
        assert s.get(User, pid2).is_admin is True


def test_approve_defaults_role_to_member(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="p3@umu.se", status="pending", oidc_sub="s3")
    assert c.post(f"/api/admin/users/{pid}/approve", json={}).status_code == 200
    with pg_session_factory() as s:
        assert s.get(User, pid).is_admin is False


def test_approve_rejects_non_pending(make_app, monkeypatch, pg_url, pg_session_factory):
    """HOLLOW UNTIL 2026-10-28 (review A2 re-proved it): with the pending guard
    DELETED this test stayed green, because the admin row has no `oidc_sub` and
    the request carried no `initial_password`, so the NEXT guard answered 400
    with "initial password required". The 400 never came from the thing the test
    is named after.

    Fix: send a valid `initial_password`, so the only guard that can produce a
    400 is the pending check — and pin the detail string and the row state, so a
    400 from some third source cannot stand in for it either.
    """
    app = _setup(make_app, monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)  # admin is active, not pending
    r = c.post(
        f"/api/admin/users/{aid}/approve",
        json={"role": "member", "initial_password": "initialpassword12"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "user is not pending", r.text
    with pg_session_factory() as s:
        u = s.get(User, aid)
        assert u.password_hash is None, "a refused approve must not issue a credential"
        assert u.approved_at is None and u.approved_by_user_id is None
        assert u.session_epoch in (0, None), "a refused approve must not bump the epoch"


def test_approve_invalid_role_on_nonexistent_is_400_not_404(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    # ordering lock: role is validated BEFORE the user lookup.
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/users/999999/approve", json={"role": "galaxy"})
    assert r.status_code == 400


def test_approve_404_for_unknown_user(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/users/999999/approve", json={"role": "member"})
    assert r.status_code == 404


def test_approve_local_requires_password_then_bumps_epoch(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="local@umu.se", status="pending")  # no oidc, no password
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


def test_approve_oauth_without_password_does_not_bump_epoch(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="g@umu.se", status="pending", oidc_sub="s9")
    with pg_session_factory() as s:
        ep0 = s.get(User, pid).session_epoch
    assert c.post(f"/api/admin/users/{pid}/approve", json={"role": "member"}).status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "active"
        assert u.session_epoch == ep0  # passwordless OAuth approve does NOT bump epoch


def test_approve_weak_password_400(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        p = User(email="local2@umu.se", status="pending")
        s.add(p)
        s.commit()
        s.refresh(p)
        pid = p.id
    r = c.post(
        f"/api/admin/users/{pid}/approve",
        json={"role": "member", "initial_password": "short"},
    )
    assert r.status_code == 400


def test_deny_only_pending(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    pid = _user(pg_session_factory, email="d@umu.se", status="pending", oidc_sub="sd")
    with pg_session_factory() as s:
        ep0 = s.get(User, pid).session_epoch
    assert c.post(f"/api/admin/users/{pid}/deny").status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, pid)
        assert u.status == "disabled"
        assert u.session_epoch == ep0  # deny does NOT bump epoch
    # denying a non-pending user -> 400
    uid = _user(pg_session_factory, email="active2@umu.se", status="active")
    assert c.post(f"/api/admin/users/{uid}/deny").status_code == 400


def test_deny_404(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/deny").status_code == 404


def test_disable_bumps_epoch(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    _user(
        pg_session_factory, email="admin2@umu.se", status="active", is_admin=True
    )  # keep >1 admin
    with pg_session_factory() as s:
        m = User(email="m@umu.se", status="active")
        s.add(m)
        s.commit()
        s.refresh(m)
        mid, before = m.id, m.session_epoch
    assert c.post(f"/api/admin/users/{mid}/disable").status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, mid)
        assert u.status == "disabled" and u.session_epoch == before + 1


def test_disable_only_active_400(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="dis@umu.se", status="disabled")
    assert c.post(f"/api/admin/users/{did}/disable").status_code == 400


def test_cannot_disable_last_active_admin(make_app, monkeypatch, pg_url, pg_session_factory):
    # Parity: the sole active admin disabling their OWN account hits the
    # last-admin guard -> 400 (no self-disable special-case exists).
    app = _setup(make_app, monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)
    assert c.post(f"/api/admin/users/{aid}/disable").status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).status == "active"


def test_disable_404(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/disable").status_code == 404


def test_change_role_to_admin_and_member(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)  # keeps an admin around for demote tests
    uid = _user(pg_session_factory, email="r@umu.se", status="active", is_admin=False)
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


def test_change_role_demote_last_admin_guarded(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, aid = _admin(app, pg_session_factory)  # only one active admin
    r = c.post(f"/api/admin/users/{aid}/role", json={"role": "member"})
    assert r.status_code == 400
    with pg_session_factory() as s:
        assert s.get(User, aid).is_admin is True


def test_change_role_invalid_role_on_nonexistent_is_400(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/role", json={"role": "galaxy"}).status_code == 400


def test_change_role_not_active_400(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="rd@umu.se", status="disabled")
    assert c.post(f"/api/admin/users/{did}/role", json={"role": "admin"}).status_code == 400


def test_enable_only_disabled(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="en@umu.se", status="disabled")
    with pg_session_factory() as s:
        ep0 = s.get(User, did).session_epoch
    assert c.post(f"/api/admin/users/{did}/enable").status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, did)
        assert u.status == "active"
        assert u.session_epoch == ep0  # enable does NOT bump epoch
    # enabling a non-disabled (pending) user -> 400
    pid = _user(pg_session_factory, email="enp@umu.se", status="pending", oidc_sub="sp")
    assert c.post(f"/api/admin/users/{pid}/enable").status_code == 400


def test_enable_404(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    assert c.post("/api/admin/users/999999/enable").status_code == 404


def test_reset_password_bumps_epoch(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    with pg_session_factory() as s:
        u = User(email="rp@umu.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid, ep0 = u.id, u.session_epoch
    r = c.post(f"/api/admin/users/{uid}/reset-password", json={"new_password": "fresh-strong-pw"})
    assert r.status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert verify_password(u.password_hash, "fresh-strong-pw") and u.session_epoch == ep0 + 1


def test_reset_rejects_weak_400(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    uid = _user(pg_session_factory, email="rp2@umu.se", status="active")
    r = c.post(f"/api/admin/users/{uid}/reset-password", json={"new_password": "short"})
    assert r.status_code == 400


def test_reset_password_has_no_status_gate(make_app, monkeypatch, pg_url, pg_session_factory):
    # reset-password works for a DISABLED user (no status gate) — pins the non-gate invariant.
    #
    # Review 2026-09-02 A6 F2: this asserted only `== 200`, which an
    # implementation that KEEPS a status gate and answers a disabled user with a
    # 200 no-op (early return before the hash write) also satisfies. The two
    # side-effect assertions below are the ones the active-user sibling
    # (test_reset_password_bumps_epoch) already makes; the whole point of this
    # test is that a disabled user gets the same treatment, so it must check the
    # same things.
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    did = _user(pg_session_factory, email="rpd@umu.se", status="disabled")
    with pg_session_factory() as s:
        ep0 = s.get(User, did).session_epoch
    r = c.post(f"/api/admin/users/{did}/reset-password", json={"new_password": "fresh-strong-pw"})
    assert r.status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, did)
        assert verify_password(u.password_hash, "fresh-strong-pw"), (
            "responded 200 but never wrote the new credential"
        )
        assert u.session_epoch == ep0 + 1, "the reset did not revoke existing sessions"
        assert u.status == "disabled", "the reset must not quietly re-enable the account"


def test_reset_404(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    r = c.post("/api/admin/users/999999/reset-password", json={"new_password": "fresh-strong-pw"})
    assert r.status_code == 404


# --- Review 2026-09-02 A11 F1: admins can release a squatted address ---------


def test_release_deletes_a_pending_row(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    uid = _user(pg_session_factory, email="squat@umu.se", status="pending")
    r = c.post(f"/api/admin/users/{uid}/release")
    assert r.status_code == 200 and r.json() == {"id": uid}
    with pg_session_factory() as s:
        assert s.get(User, uid) is None


def test_release_refuses_anything_but_a_pending_row(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    app = _setup(make_app, monkeypatch, pg_url)
    c, _ = _admin(app, pg_session_factory)
    active = _user(pg_session_factory, email="active@umu.se", status="active")
    disabled = _user(pg_session_factory, email="off@umu.se", status="disabled")
    assert c.post(f"/api/admin/users/{active}/release").status_code == 400
    assert c.post(f"/api/admin/users/{disabled}/release").status_code == 400
    assert c.post("/api/admin/users/999999/release").status_code == 404
    with pg_session_factory() as s:
        assert s.get(User, active) is not None and s.get(User, disabled) is not None


def test_release_is_admin_only(make_app, monkeypatch, pg_url, pg_session_factory):
    app = _setup(make_app, monkeypatch, pg_url)
    victim = _user(pg_session_factory, email="v@umu.se", status="pending")
    member = _user(pg_session_factory, email="m@umu.se", status="active")
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=member))
    assert c.post(f"/api/admin/users/{victim}/release").status_code == 403
