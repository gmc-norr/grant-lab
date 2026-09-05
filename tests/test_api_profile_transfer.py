"""`PUT /api/profiles/{profile_id}/owner` -- profile ownership transfer.

Thirteen cases. `created_by` is load-bearing in seven places (profile_access.py:69,
profiles.py:142/196/400/544/612, calls.py:641), so this endpoint is one transaction,
not a field update, and each case below pins one decision from D10/D9/Open-6/Open-7/D17.

FIXTURES -- this repo has NO `db_session`, `client` or `client_unauth`. The session
fixture is `pg_session_factory` (tests/conftest.py:151), a FACTORY used as
`with pg_session_factory() as s:`. The authenticated-request pattern is
`_setup(monkeypatch, pg_url) -> app`, then a `TestClient` with
`app.state.session_signer.make(user_id=uid)` in the `session` cookie -- copied from
tests/test_api_profile_detail.py:8-20.

`test_a_stranger_gets_404_not_403` and its sibling assert the response BODY, not just
the status code: a missing route also answers 404
(`{"error":{"code":"not_found","message":"Not Found"}}`), so a status-only assertion
is green before a line of the endpoint exists. The real refusal comes from
`_profile_for_edit` (profiles.py:139/141), which raises `detail="profile not found"`,
rendered into the envelope's `message` by main.py:548.
"""

from fastapi.testclient import TestClient

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.db.models import Profile, ProfileKind, ProfileViewer, Subscription, User
from grant_lab.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def _user(session, email, *, is_admin=False, status="active", name=None):
    u = User(email=email, name=name or email, status=status, is_admin=is_admin)
    session.add(u)
    session.flush()
    return u


def _profile(session, owner, *, visibility="private", name="P"):
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


def test_owner_can_transfer_to_an_active_user(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner@umu.se")
        new_owner = _user(s, "newowner@umu.se")
        p = _profile(s, owner)
        s.commit()
        pid, new_owner_id = p.id, new_owner.id

    client = _client_for(app, owner.id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner@umu.se"})
    assert r.status_code == 200, r.text
    body = r.json()
    # ProfileTransferOut.owner reuses ProfileViewerOut (Step 3's verbatim code),
    # whose field is `user_id`, not `id` -- see the plan-defect note in the
    # task report about the "Produces" prose disagreeing with the code block.
    assert body["owner"]["user_id"] == new_owner_id
    assert body["owner"]["email"] == "newowner@umu.se"

    with pg_session_factory() as s:
        assert s.get(Profile, pid).created_by == new_owner_id


def test_admin_can_transfer_a_profile_they_do_not_own(monkeypatch, pg_url, pg_session_factory):
    """D10: the point. An owner who can no longer log in must still be fixable."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner2@umu.se")
        admin = _user(s, "admin@umu.se", is_admin=True)
        new_owner = _user(s, "newowner2@umu.se")
        p = _profile(s, owner)
        s.commit()
        pid, admin_id, new_owner_id = p.id, admin.id, new_owner.id

    client = _client_for(app, admin_id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner2@umu.se"})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        assert s.get(Profile, pid).created_by == new_owner_id


def test_a_stranger_gets_404_not_403(monkeypatch, pg_url, pg_session_factory):
    """No existence oracle: a stranger cannot tell 'not shared' from 'does not exist'.

    Positive control in the same test: the owner's identical request returns 200,
    so 'the route exists' is inside the gate rather than assumed.
    """
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner3@umu.se")
        stranger = _user(s, "stranger3@umu.se")
        _user(s, "newowner3@umu.se")
        p = _profile(s, owner)
        s.commit()
        pid, owner_id, stranger_id = p.id, owner.id, stranger.id

    stranger_client = _client_for(app, stranger_id)
    with stranger_client:
        r = stranger_client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner3@umu.se"})
    assert r.status_code == 404 and b"profile not found" in r.content, r.text

    owner_client = _client_for(app, owner_id)
    with owner_client:
        r_owner = owner_client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner3@umu.se"})
    assert r_owner.status_code == 200, r_owner.text


def test_a_reader_who_cannot_edit_gets_403(monkeypatch, pg_url, pg_session_factory):
    """Reading is wider than editing: a shared viewer reaches 403, not 404."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner4@umu.se")
        reader = _user(s, "reader4@umu.se")
        _user(s, "newowner4@umu.se")
        p = _profile(s, owner)
        s.flush()
        s.add(ProfileViewer(profile_id=p.id, user_id=reader.id))
        s.commit()
        pid, reader_id = p.id, reader.id

    client = _client_for(app, reader_id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner4@umu.se"})
    assert r.status_code == 403, r.text


def test_an_inactive_recipient_is_refused_generically(monkeypatch, pg_url, pg_session_factory):
    """Open-8: an inactive account is not a valid transfer target, and the
    caller is not told WHY -- see the byte-equal comparison below."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner5@umu.se")
        _user(s, "disabled5@umu.se", status="disabled")
        p = _profile(s, owner)
        s.commit()
        pid, owner_id = p.id, owner.id

    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": "disabled5@umu.se"})
    assert r.status_code == 400, r.text


def test_an_unknown_email_gets_the_SAME_generic_400(monkeypatch, pg_url, pg_session_factory):
    """Two different messages ('no such user' vs 'inactive') would make this
    endpoint a directory of who has an account. Both refusals must be the exact
    same response body, not merely both 400 -- asserted here byte-for-byte,
    inside the SAME test, against the SAME client, so nothing about test
    ordering or fixture drift can make two different messages look equal.
    """
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner6@umu.se")
        _user(s, "disabled6@umu.se", status="disabled")
        p = _profile(s, owner)
        s.commit()
        pid, owner_id = p.id, owner.id

    client = _client_for(app, owner_id)
    with client:
        r_inactive = client.put(f"/api/profiles/{pid}/owner", json={"email": "disabled6@umu.se"})
        r_unknown = client.put(f"/api/profiles/{pid}/owner", json={"email": "nobody-at-all@umu.se"})
    assert r_inactive.status_code == 400, r_inactive.text
    assert r_unknown.status_code == 400, r_unknown.text
    assert r_inactive.content == r_unknown.content


def test_the_incoming_owners_viewer_row_is_deleted(monkeypatch, pg_url, pg_session_factory):
    """D10's stale-row hazard: an active non-owner viewer becomes the owner, and
    the ProfileViewer docstring's invariant ('the OWNER never has a row') must
    stay true afterwards.
    """
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner7@umu.se")
        new_owner = _user(s, "newowner7@umu.se")
        p = _profile(s, owner)
        s.flush()
        s.add(ProfileViewer(profile_id=p.id, user_id=new_owner.id))
        s.commit()
        pid, new_owner_id, owner_id = p.id, new_owner.id, owner.id

    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner7@umu.se"})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        row = s.query(ProfileViewer).filter_by(profile_id=pid, user_id=new_owner_id).one_or_none()
        assert row is None


def test_the_new_owner_gets_a_subscription_row(monkeypatch, pg_url, pg_session_factory):
    """D9 depends on it: nobody is notified about a profile with no subscription."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner8@umu.se")
        new_owner = _user(s, "newowner8@umu.se")
        p = _profile(s, owner)
        s.commit()
        pid, new_owner_id, owner_id = p.id, new_owner.id, owner.id

    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner8@umu.se"})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        row = s.query(Subscription).filter_by(profile_id=pid, user_id=new_owner_id).one_or_none()
        assert row is not None
        assert row.digest_enabled is True


def test_the_old_owners_subscription_row_SURVIVES(monkeypatch, pg_url, pg_session_factory):
    """digest/assemble.py:86-88: deleting the outgoing owner's row destroys their
    digest_enabled preference and a re-share cannot restore it. Give the old
    owner a NON-default preference so a blind re-create-with-defaults would be
    caught too.
    """
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner9@umu.se")
        _user(s, "newowner9@umu.se")
        p = _profile(s, owner)
        s.flush()
        s.add(Subscription(user_id=owner.id, profile_id=p.id, digest_enabled=False))
        s.commit()
        pid, owner_id = p.id, owner.id

    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner9@umu.se"})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        row = s.query(Subscription).filter_by(profile_id=pid, user_id=owner_id).one_or_none()
        assert row is not None
        assert row.digest_enabled is False


def test_keep_read_access_writes_a_viewer_row_for_the_outgoing_owner(
    monkeypatch, pg_url, pg_session_factory
):
    """Open-6: `keep_read_access=True` adds a ProfileViewer row for the outgoing
    owner of a private profile, so they are not silently locked out."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner10@umu.se")
        _user(s, "newowner10@umu.se")
        p = _profile(s, owner, visibility="private")
        s.commit()
        pid, owner_id = p.id, owner.id

    client = _client_for(app, owner_id)
    with client:
        r = client.put(
            f"/api/profiles/{pid}/owner",
            json={"email": "newowner10@umu.se", "keep_read_access": True},
        )
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        row = s.query(ProfileViewer).filter_by(profile_id=pid, user_id=owner_id).one_or_none()
        assert row is not None


def test_a_now_unreadable_default_profile_id_is_cleared(monkeypatch, pg_url, pg_session_factory):
    """Open-7: a private profile the outgoing owner no longer has ANY read
    access to must not stay their default_profile_id -- that would point the
    dashboard at a profile they can no longer open."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner11@umu.se")
        _user(s, "newowner11@umu.se")
        p = _profile(s, owner, visibility="private")
        s.flush()
        owner.default_profile_id = p.id
        s.commit()
        pid, owner_id = p.id, owner.id

    client = _client_for(app, owner_id)
    with client:
        r = client.put(f"/api/profiles/{pid}/owner", json={"email": "newowner11@umu.se"})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        assert s.get(User, owner_id).default_profile_id is None


def test_a_still_readable_default_profile_id_is_LEFT(monkeypatch, pg_url, pg_session_factory):
    """The other half of the Open-7 pair, on purpose: a rule that clears
    unconditionally passes the case above and fails this one. Here the outgoing
    owner keeps read access (keep_read_access=True), so default_profile_id must
    survive untouched."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner12@umu.se")
        _user(s, "newowner12@umu.se")
        p = _profile(s, owner, visibility="private")
        s.flush()
        owner.default_profile_id = p.id
        s.commit()
        pid, owner_id = p.id, owner.id

    client = _client_for(app, owner_id)
    with client:
        r = client.put(
            f"/api/profiles/{pid}/owner",
            json={"email": "newowner12@umu.se", "keep_read_access": True},
        )
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        assert s.get(User, owner_id).default_profile_id == pid


def test_the_detail_payload_says_whether_the_owner_would_lose_read(
    monkeypatch, pg_url, pg_session_factory
):
    """D17: the transfer UI must say so BEFORE the act, so this must exist on
    GET /api/profiles/{id} without going through the transfer at all. A
    hardcoded True fails the admin case; a hardcoded False fails the non-admin
    case."""
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = _user(s, "owner13@umu.se")
        admin_owner = _user(s, "adminowner13@umu.se", is_admin=True)
        p1 = _profile(s, owner, visibility="private", name="P1")
        p2 = _profile(s, admin_owner, visibility="private", name="P2")
        s.commit()
        pid1, owner_id = p1.id, owner.id
        pid2, admin_id = p2.id, admin_owner.id

    non_admin_client = _client_for(app, owner_id)
    with non_admin_client:
        r1 = non_admin_client.get(f"/api/profiles/{pid1}")
    assert r1.status_code == 200, r1.text
    assert r1.json()["outgoing_lost_read"] is True

    admin_client = _client_for(app, admin_id)
    with admin_client:
        r2 = admin_client.get(f"/api/profiles/{pid2}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["outgoing_lost_read"] is False
