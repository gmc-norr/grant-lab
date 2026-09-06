"""Two concurrent sharing mutations on one profile row.

FIXTURES: this repo has no `client`, `session_factory`, `owner_profile`,
`auth_headers` or `make_user`. Auth is a signed session COOKIE, not a header.
Module-local helpers, in the idiom of tests/test_api_profile_transfer.py:32-66.
"""

import threading

from fastapi.testclient import TestClient

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.main import create_app

# Hard bound on every join AND on the barrier. pyproject.toml's 120s
# pytest-timeout uses method=signal, which interrupts only the MAIN thread --
# a worker wedged inside a blocked `SELECT ... FOR UPDATE` is not interrupted
# by it and can hold an xdist worker's Postgres connection open. A bounded
# join turns a future regression into a legible failure instead of a stall.
JOIN_TIMEOUT = 30


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    c = TestClient(app)
    c.cookies.set("session", signer.make(user_id=uid))
    return c


def _user(session, email):
    u = User(email=email, name=email, status="active")
    session.add(u)
    session.flush()
    return u


def _profile(session, owner):
    p = Profile(name="P", kind=ProfileKind.ORGANIZATIONAL_UNIT, level="group",
                description="", structured={}, created_by=owner.id, visibility="private")
    session.add(p)
    session.flush()
    return p


def _run_two(fns):
    """Fire both under a barrier, joining with a HARD TIMEOUT.

    The barrier needs its own timeout as well as the joins: a bare
    threading.Barrier(2) blocks FOREVER if one thread dies before reaching it,
    which is the realistic way this harness hangs -- an exception in setup, not
    in the lock. (The row lock itself is not a deadlock hazard here: the
    barrier sits BEFORE the request, so no thread ever waits on it while
    holding or requesting the lock. Measured across 12 runs, longest 7.0s.)
    """
    barrier = threading.Barrier(2, timeout=JOIN_TIMEOUT)
    errors = []

    def wrap(fn):
        def go():
            try:
                barrier.wait()
                fn()
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        return go

    # daemon=True is load-bearing, not hygiene. The joins below are bounded and
    # the callers assert `not is_alive()`, so a wedged worker becomes a legible
    # FAILED -- but a non-daemon thread is then joined with NO timeout by
    # CPython's threading._shutdown() at interpreter exit, so the very
    # regression this file exists to catch would stall the xdist worker forever
    # after reporting its red. Daemon threads are abandoned at exit instead.
    ts = [threading.Thread(target=wrap(f), daemon=True) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=JOIN_TIMEOUT)
    return [t.is_alive() for t in ts], errors


def test_two_concurrent_transfers_do_not_both_authorize_against_the_old_owner(
    monkeypatch, pg_url, pg_session_factory
):
    """Exactly ONE transfer may succeed. Without FOR UPDATE both readers load
    owner A, both pass the authorization check, and both commit -- the profile
    silently moves twice and each response reports outgoing_lost_read for an
    owner that no longer holds it.

    COUNT the successes. Do NOT assert that some 200 response named the final
    owner: the last thread to commit writes its own target and returns that
    same value in its own body, so such a winner exists BY CONSTRUCTION whether
    or not the lock is there. That assertion is a tautology about
    last-writer-wins, and it passed the unfixed tree.

    The loser's status is 404, not 403, and that is correct: under READ
    COMMITTED the blocked SELECT ... FOR UPDATE re-reads the newly committed
    row, so the loser sees a private profile it no longer owns, and
    `_profile_for_edit` checks can_read_profile BEFORE ownership
    (profiles.py:150-153). "Reading stays WIDER than editing"
    (profiles.py:144-145). Do not "correct" this to 403.
    """
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner@umu.se")
        b = _user(s, "b@umu.se")
        c = _user(s, "c@umu.se")
        p = _profile(s, owner)
        s.commit()
        pid, oid, b_email, c_email = p.id, owner.id, b.email, c.email

    results = []
    client = _client_for(app, oid)

    def transfer(email):
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": email})
        results.append((email, r.status_code, r.json() if r.status_code == 200 else None))

    alive, errors = _run_two([lambda: transfer(b_email), lambda: transfer(c_email)])
    assert not any(alive), f"HUNG: threads still alive after {JOIN_TIMEOUT}s"
    assert not errors, errors

    with pg_session_factory() as s:
        final = s.get(Profile, pid).created_by

    ok = [r for r in results if r[1] == 200]
    assert len(ok) == 1, (
        f"both transfers authorized against owner {oid}: "
        f"statuses={[r[1] for r in results]}, final={final}"
    )


def test_a_transfer_and_a_viewer_add_do_not_error(monkeypatch, pg_url, pg_session_factory):
    """A no-500, no-hang smoke check across two DIFFERENT endpoints that share
    _profile_for_edit.

    It is NOT the control proving the lock lives in the helper. Its only
    observable is a raised exception, and a 403 or 404 is a RESPONSE, not an
    exception -- so it stays green under an implementation that locked only the
    transfer handler, which is exactly the mutant such a control would exist to
    catch. Measured identical in both states: codes=[('viewer', 200),
    ('transfer', 200)]. Step 5's grep is the real structural control.

    Do NOT "strengthen" this to `len(ok) == 1`: the winner is genuinely
    order-nondeterministic between two different endpoints, so that assertion
    would be flaky.
    """
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner2@umu.se")
        b = _user(s, "b2@umu.se")
        v = _user(s, "v2@umu.se")
        p = _profile(s, owner)
        s.commit()
        pid, oid, b_email, v_email = p.id, owner.id, b.email, v.email

    client = _client_for(app, oid)
    codes = []
    alive, errors = _run_two([
        lambda: codes.append(("transfer", client.put(
            f"/api/profiles/{pid}/owner", json={"email": b_email}).status_code)),
        lambda: codes.append(("viewer", client.post(
            f"/api/profiles/{pid}/viewers", json={"email": v_email}).status_code)),
    ])
    assert not any(alive), f"HUNG: threads still alive after {JOIN_TIMEOUT}s"
    assert not errors, f"a concurrent sharing mutation raised: {errors}"
