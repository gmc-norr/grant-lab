"""Credential and session-epoch writers must commit one at a time per user.

Security review 2026-10-31 S1: a self-service password change, paused after it
verified the old password, resumed after an administrator reset had committed.
It overwrote the reset password and minted a cookie under the epoch the reset
had just written, so the old credential beat the account-recovery boundary.

Every route that writes `password_hash` or `session_epoch` from a value it read
now loads the user row with SELECT ... FOR UPDATE (auth.accounts.lock_user).
Login is one of them: its argon2 rehash wrote the OLD password's hash back
after a reset had committed.

FIXTURES: auth is a signed session COOKIE. Clients are built WITHOUT
`with TestClient(app):` -- the lifespan portal serializes threads, which would
hide every race in this file.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from grant_lab.auth.passwords import hash_password, verify_password
from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import User

# Bounded joins: pytest-timeout's signal method only interrupts the main
# thread, so a worker stuck on a row lock would otherwise stall the xdist worker.
JOIN_TIMEOUT = 30

OLD_PW = "old-password-123"
SELF_PW = "self-chosen-pass-1"
RESET_PW = "admin-reset-pass-1"


def _setup(make_app, monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return make_app()


def _client(app, uid=None, epoch=0):
    c = TestClient(app)
    if uid is not None:
        signer: SessionSigner = app.state.session_signer
        c.cookies.set("session", signer.make(user_id=uid, session_epoch=epoch))
    return c


def _seed(sf, *, status="active", password=OLD_PW):
    with sf() as s:
        admin = User(email="admin@umu.se", status="active", is_admin=True, session_epoch=0)
        target = User(
            email="target@umu.se",
            status=status,
            session_epoch=0,
            password_hash=hash_password(password) if password else None,
        )
        s.add_all([admin, target])
        s.commit()
        return admin.id, target.id


def _lock_waiters(sf) -> int:
    with sf() as s:
        return s.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ).scalar_one()


def _wait_for(cond, done: threading.Event) -> bool:
    """Poll until cond() is true or `done` fires. Observed, not timed."""
    deadline = time.monotonic() + JOIN_TIMEOUT
    while time.monotonic() < deadline and not done.is_set():
        if cond():
            return True
        time.sleep(0.05)
    return False


def _password_is(sf, uid, pw) -> bool:
    with sf() as s:
        ok, _ = verify_password(s.get(User, uid).password_hash, pw)
        return ok


def _thread(fn):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


# --- every writer takes the row lock ----------------------------------------


def _call_me_password(app, aid, uid):
    return _client(app, uid).post(
        "/api/me/password", json={"current_password": OLD_PW, "new_password": SELF_PW}
    )


def _call_admin_reset(app, aid, uid):
    return _client(app, aid).post(
        f"/api/admin/users/{uid}/reset-password", json={"new_password": RESET_PW}
    )


def _call_admin_approve(app, aid, uid):
    return _client(app, aid).post(
        f"/api/admin/users/{uid}/approve",
        json={"role": "member", "initial_password": RESET_PW},
    )


def _call_admin_disable(app, aid, uid):
    return _client(app, aid).post(f"/api/admin/users/{uid}/disable")


def _call_logout(app, aid, uid):
    return _client(app, uid).post("/api/auth/logout")


def _call_login(app, aid, uid):
    return _client(app).post("/api/auth/login", json={"email": "target@umu.se", "password": OLD_PW})


BLOCKER_PW = "blocker-pass-123"

# (call, target status, expected HTTP status, expected final epoch, password after)
# The blocker commits epoch 5 and BLOCKER_PW while the writer waits. A writer
# that read the row BEFORE taking the lock acts on epoch 0 and the old hash.
WRITERS = [
    # the session (epoch 0) was revoked under it: refuse, change nothing
    pytest.param(_call_me_password, "active", 401, 5, BLOCKER_PW, id="me-password"),
    pytest.param(_call_admin_reset, "active", 200, 6, RESET_PW, id="admin-reset"),
    pytest.param(_call_admin_approve, "pending", 200, 6, RESET_PW, id="admin-approve"),
    pytest.param(_call_admin_disable, "active", 200, 6, BLOCKER_PW, id="admin-disable"),
    # the cookie (epoch 0) is already dead: no bump, and never back to 1
    pytest.param(_call_logout, "active", 200, 5, BLOCKER_PW, id="logout"),
    # the old password stopped working while the login waited
    pytest.param(_call_login, "active", 400, 5, BLOCKER_PW, id="login"),
]


@pytest.mark.parametrize(("call", "status", "code", "epoch", "password"), WRITERS)
def test_every_credential_writer_acts_on_the_row_it_locked(
    call, status, code, epoch, password, make_app, monkeypatch, pg_url, pg_session_factory
):
    """A writer must read the row it writes UNDER the lock.

    Waiting is not enough: an unlocked reader also waits, at its UPDATE, and
    then writes a value derived from what it read before. So the blocker
    changes the row -- epoch 5, a new password -- and commits only once the
    writer is waiting. The writer's outcome must reflect that commit: the epoch
    only ever grows, and a credential check runs against the current hash.
    """
    app = _setup(make_app, monkeypatch, pg_url)
    aid, uid = _seed(pg_session_factory, status=status)

    blocker = pg_session_factory()
    blocker.execute(
        text("UPDATE users SET session_epoch = 5, password_hash = :h WHERE id = :id"),
        {"h": hash_password(BLOCKER_PW), "id": uid},
    )

    result = {}
    done = threading.Event()

    def go():
        try:
            result["status"] = call(app, aid, uid).status_code
        finally:
            done.set()

    t = _thread(go)
    try:
        waited = _wait_for(lambda: _lock_waiters(pg_session_factory) > 0, done)
        blocker.commit()
    finally:
        blocker.close()
        t.join(timeout=JOIN_TIMEOUT)

    assert not t.is_alive(), f"HUNG after {JOIN_TIMEOUT}s"
    assert waited, "the writer never waited on the row"
    with pg_session_factory() as s:
        final_epoch = s.get(User, uid).session_epoch
    assert (result["status"], final_epoch) == (code, epoch)
    assert _password_is(pg_session_factory, uid, password)


# --- the S1 reproduction -----------------------------------------------------


def test_an_old_password_change_in_flight_cannot_undo_an_admin_reset(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    """The review's four steps, with the fix's expected outcome.

    1. /api/me/password verifies the old password and pauses in hashing.
    2. An admin resets the password.
    3. The change resumes.
    The reset must land LAST: the admin's password works, the self-chosen one
    does not, and the cookie the change minted is dead.
    """
    from grant_lab.web.api import me as me_api

    app = _setup(make_app, monkeypatch, pg_url)
    aid, uid = _seed(pg_session_factory)

    paused, release = threading.Event(), threading.Event()
    real_hash = me_api.hash_password

    def pausing_hash(pw):
        paused.set()
        release.wait(timeout=JOIN_TIMEOUT)
        return real_hash(pw)

    monkeypatch.setattr(me_api, "hash_password", pausing_hash)

    results = {}
    change_client = _client(app, uid)
    reset_done = threading.Event()

    def change():
        r = change_client.post(
            "/api/me/password", json={"current_password": OLD_PW, "new_password": SELF_PW}
        )
        results["change"] = r.status_code

    def reset():
        results["reset"] = _call_admin_reset(app, aid, uid).status_code
        reset_done.set()

    tc = _thread(change)
    assert paused.wait(timeout=JOIN_TIMEOUT), "the change never reached hashing"
    tr = _thread(reset)
    try:
        _wait_for(lambda: _lock_waiters(pg_session_factory) > 0, reset_done)
        reset_finished_first = reset_done.is_set()
    finally:
        release.set()
        tc.join(timeout=JOIN_TIMEOUT)
        tr.join(timeout=JOIN_TIMEOUT)

    assert not tc.is_alive() and not tr.is_alive(), f"HUNG after {JOIN_TIMEOUT}s"
    assert not reset_finished_first, "the reset committed while the old-password change was paused"
    assert results == {"change": 200, "reset": 200}
    assert _password_is(pg_session_factory, uid, RESET_PW)
    assert not _password_is(pg_session_factory, uid, SELF_PW)
    # The cookie the change re-minted was issued before the reset: it must be dead.
    assert change_client.get("/api/me").status_code == 401


def test_a_session_revoked_mid_request_cannot_change_the_password(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    """Authorization is rechecked under the lock, not only at the dependency.

    The request passes get_current_user, then a logout (same cookie, another
    tab) revokes the session. Logout leaves the password unchanged, so only
    an epoch check inside the transaction can refuse the change -- without it
    a revoked session would set a new password and get a fresh valid cookie.
    """
    from grant_lab.web.api.me import _PASSWORD_CHANGE_KEY_PREFIX

    app = _setup(make_app, monkeypatch, pg_url)
    _, uid = _seed(pg_session_factory)

    paused, release = threading.Event(), threading.Event()
    limiter = app.state.email_limiter
    real_allow = limiter.allow

    def pausing_allow(key):
        if key.startswith(_PASSWORD_CHANGE_KEY_PREFIX):
            paused.set()
            release.wait(timeout=JOIN_TIMEOUT)
        return real_allow(key)

    monkeypatch.setattr(limiter, "allow", pausing_allow)

    results = {}
    change_client = _client(app, uid)

    def change():
        r = change_client.post(
            "/api/me/password", json={"current_password": OLD_PW, "new_password": SELF_PW}
        )
        results["change"] = r.status_code

    tc = _thread(change)
    try:
        assert paused.wait(timeout=JOIN_TIMEOUT), "the change never reached its limiter"
        assert _call_logout(app, None, uid).status_code == 200
    finally:
        release.set()
        tc.join(timeout=JOIN_TIMEOUT)

    assert not tc.is_alive(), f"HUNG after {JOIN_TIMEOUT}s"
    assert results["change"] == 401
    assert _password_is(pg_session_factory, uid, OLD_PW)
    assert change_client.get("/api/me").status_code == 401


def test_a_login_rehash_in_flight_cannot_restore_the_old_password(
    make_app, monkeypatch, pg_url, pg_session_factory
):
    """Login rewrites the hash when argon2 parameters change. Unlocked, that
    write put the OLD password back over an admin reset that committed while
    the login was between its verify and its commit."""
    from grant_lab.web.api import auth as auth_api

    app = _setup(make_app, monkeypatch, pg_url)
    aid, uid = _seed(pg_session_factory)

    paused, release = threading.Event(), threading.Event()
    real_verify = auth_api.verify_password

    def verify_then_pause(stored, pw):
        ok, _ = real_verify(stored, pw)
        paused.set()
        release.wait(timeout=JOIN_TIMEOUT)
        return ok, True  # force the rehash branch

    monkeypatch.setattr(auth_api, "verify_password", verify_then_pause)

    results = {}
    login_client = _client(app)
    reset_done = threading.Event()

    def login():
        r = login_client.post(
            "/api/auth/login", json={"email": "target@umu.se", "password": OLD_PW}
        )
        results["login"] = r.status_code

    def reset():
        results["reset"] = _call_admin_reset(app, aid, uid).status_code
        reset_done.set()

    tl = _thread(login)
    assert paused.wait(timeout=JOIN_TIMEOUT), "the login never reached its verify"
    tr = _thread(reset)
    try:
        _wait_for(lambda: _lock_waiters(pg_session_factory) > 0, reset_done)
        reset_finished_first = reset_done.is_set()
    finally:
        release.set()
        tl.join(timeout=JOIN_TIMEOUT)
        tr.join(timeout=JOIN_TIMEOUT)

    assert not tl.is_alive() and not tr.is_alive(), f"HUNG after {JOIN_TIMEOUT}s"
    assert not reset_finished_first, "the reset committed while a login held the old password"
    assert results == {"login": 200, "reset": 200}
    assert _password_is(pg_session_factory, uid, RESET_PW)
    assert not _password_is(pg_session_factory, uid, OLD_PW)
    assert login_client.get("/api/me").status_code == 401
