"""Creation must not confirm a profile name the caller cannot read.

FIXTURES: no `client`, `session`, `make_user`, `make_profile` or
`auth_headers_for` exist. Auth is a signed session COOKIE. Module-local
helpers, in the idiom of tests/test_api_profile_transfer.py and
tests/test_api_profiles_create.py.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, ProfileKind, User
from grant_lab.main import create_app


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


def _profile(session, owner, *, name, visibility="private"):
    p = Profile(
        name=name,
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        level="group",
        description="",
        structured={},
        created_by=owner.id,
        visibility=visibility,
    )
    session.add(p)
    session.flush()
    return p


def test_a_name_held_by_an_unreadable_private_profile_does_not_409(
    monkeypatch, pg_url, pg_session_factory
):
    """GATE 8 -- the load-bearing one. profiles.py:302 states the rule this
    closes: "A 403 confirms the profile exists, and profile NAMES are the thing
    worth not confirming: someone can walk /api/profiles/1..N and ENUMERATE who
    is working on what." The detail route returns 404 for that reason; creation
    used to confirm the same fact by another door."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        other = _user(s, "other@umu.se")
        theirs = _profile(s, other, name="Hepatology", visibility="private")
        me = _user(s, "me@umu.se")
        s.commit()
        me_id, other_pid = me.id, theirs.id

    client = _client_for(app, me_id)
    with client:
        # Pin the PREMISE. Without this the gate is unreadable-by-assumption:
        # if a future change made `private` broadly readable, gate 8 would keep
        # passing while proving nothing at all.
        assert client.get(f"/api/profiles/{other_pid}").status_code == 404
        r = client.post("/api/profiles", json={"name": "Hepatology", "kind": "organizational_unit"})
    assert r.status_code != 409, "creation confirmed a profile the caller cannot read"
    assert r.status_code < 400, r.text


def test_a_duplicate_name_the_caller_DOES_own_still_409s(monkeypatch, pg_url, pg_session_factory):
    """GATE 9 -- a BEHAVIOUR gate, not a mechanism gate. It pins that your own
    duplicate still 409s, which gate 8 alone would let you break by simply
    deleting the check.

    It does NOT catch a deleted pre-check on its own: profiles.py:448-449
    converts the resulting IntegrityError into the SAME 409 with the SAME
    message, so the two implementations are indistinguishable from here. The
    duplicate refusal is doubly enforced -- pre-check AND unique index -- and
    only removing BOTH reddens this. See Step 7."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        me = _user(s, "me@umu.se")
        _profile(s, me, name="Hepatology")
        s.commit()
        me_id = me.id

    client = _client_for(app, me_id)
    with client:
        r = client.post("/api/profiles", json={"name": "Hepatology", "kind": "organizational_unit"})
    assert r.status_code == 409


def test_two_owners_may_both_hold_the_same_active_name(pg_session_factory):
    """GATE 10. Without this, keeping the index global passes gate 9 and the
    schema still enforces the oracle at the DATABASE level -- where
    profiles.py:448-449 quietly re-serves it as the very same 409, so gate 8
    would go red again with no visible change in the response."""
    with pg_session_factory() as s:
        a = _user(s, "a@umu.se")
        b = _user(s, "b@umu.se")
        _profile(s, a, name="Hepatology")
        _profile(s, b, name="Hepatology")
        try:
            s.commit()  # must not raise IntegrityError
        except IntegrityError as e:
            pytest.fail(f"the global index is still in force: {str(e)[:200]}")


def test_a_transfer_into_a_name_collision_does_not_500(
    monkeypatch, pg_url, pg_session_factory
):
    """GATE 11 -- the oracle this change nearly relocated instead of closing.

    Migration 0076 put `created_by` INTO the active-name unique key, so a
    transfer can now breach it: the recipient may already own an active profile
    with this name. Before the converter, `session.commit()` raised
    UniqueViolation, main.py's catch-all returned 500, and any authenticated
    user could create a profile named X, PUT it at victim@umu.se, and read
    500-vs-200 to learn whether the victim owns an active profile named X. The
    failing case rolls back, so the probe was free and repeatable -- the exact
    fact gate 8 exists to stop confirming, served through a different door.

    Measured before the fix: `status=500 body={"error":{"code":"internal_error"
    ,"message":"internal server error"}}`.

    The 400 must be the SAME generic refusal an unknown or inactive recipient
    gets (profiles.py's "could not transfer profile"). Asserting a distinct
    message here would be asserting the oracle back into existence."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        a = _user(s, "ta@umu.se")
        b = _user(s, "tb@umu.se")
        mine = _profile(s, a, name="Shared name")
        _profile(s, b, name="Shared name")  # b ALREADY owns this active name
        s.commit()
        pid, a_id, b_email = mine.id, a.id, b.email

    client = _client_for(app, a_id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": b_email})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "could not transfer profile"

    # And the transfer did NOT happen.
    with pg_session_factory() as s:
        from grant_lab.db.models import Profile as _P
        assert s.get(_P, pid).created_by == a_id
