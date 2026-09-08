"""Gates for PUT /api/calls/{id}/note (spec 2026-09-07 decision notes, §7 G2-G5).

The note hangs off a decision row that must already exist. Every gate here
therefore creates the decision first -- a fixture that skips that step tests
the 404 arm and nothing else.
"""

from __future__ import annotations

from sqlalchemy import select

from grant_lab.db.models import CallDecision

# _seed_call, _setup and _login are copied from tests/test_api_calls_hide.py.
# Keep them identical; if that file's fixture changes, change this one too.
from tests.test_api_calls_hide import _login, _seed_call, _setup


def _decided_at(pg_session_factory, user_id: int):
    """The one decision row's decided_at, read in its own session.

    The non-flip gates below need it: an upsert regressed to
    `on_conflict_do_nothing` leaves the note standing AND returns 200, so only
    a moved decided_at distinguishes "the CASE spared the note" from "nothing
    was written".
    """
    with pg_session_factory() as s:
        return s.execute(
            select(CallDecision.decided_at).where(CallDecision.user_id == user_id)
        ).scalar_one()


def test_note_round_trips_on_a_hidden_call(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "wrong field - this is materials science"},
        )
    assert r.status_code == 200
    assert r.json() == {"decision_note": "wrong field - this is materials science"}
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(
                CallDecision.user_id == uid, CallDecision.call_id == cid
            )
        ).scalar_one()
    assert row.note == "wrong field - this is materials science"


def test_note_404s_when_there_is_no_decision(monkeypatch, pg_url, pg_session_factory):
    """G2. The endpoint must not INVENT a decision to hang the note on: `state`
    is NOT NULL and constrained to saved|hidden, so it would have to guess."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "hi"}
        )
    assert r.status_code == 404
    with pg_session_factory() as s:
        rows = s.execute(select(CallDecision)).scalars().all()
    # The row count is the gate. A 404 that still wrote is a 404 that lied.
    assert rows == []


def test_a_note_write_does_not_move_decided_at(monkeypatch, pg_url, pg_session_factory):
    """G4. Both existing decision endpoints DO set decided_at=func.now() in
    their upsert, so copying one of them is the natural mistake -- and the
    Saved/Hidden lists are ordered by decided_at, so it would reorder the list
    under a user as they annotate it."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        with pg_session_factory() as s:
            before = s.execute(
                select(CallDecision.decided_at).where(CallDecision.user_id == uid)
            ).scalar_one()
        client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "later"}
        )
    with pg_session_factory() as s:
        after = s.execute(
            select(CallDecision.decided_at).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert after == before


def test_whitespace_only_becomes_null_not_empty_string(
    monkeypatch, pg_url, pg_session_factory
):
    """D8. Otherwise "has a note" has two answers and the golden-set list
    renders blank entries that consumed a slot in the cap."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "   "}
        )
    assert r.status_code == 200
    assert r.json() == {"decision_note": None}
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert row.note is None


def test_note_over_1000_chars_is_rejected(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "x" * 1001}
        )
    assert r.status_code == 422


def test_a_note_of_exactly_1000_chars_is_accepted(monkeypatch, pg_url, pg_session_factory):
    """The ACCEPT half of the boundary. Without it, max_length=999 passes the
    reject test above -- verified 2026-09-07: lowering the constraint to 999
    left every other test in this file green."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "x" * 1000}
        )
    assert r.status_code == 200
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert len(row.note) == 1000


def test_a_colleague_cannot_overwrite_your_note_on_a_shared_profile(
    monkeypatch, pg_url, pg_session_factory
):
    """`CallDecision.user_id == user.id` in the UPDATE's WHERE is the ONLY thing
    that scopes this write to its author, and nothing pinned it.

    Profiles default to visibility "org" (web/profile_access.py), so
    `_readable_active_profile_or_404` waves any colleague through -- the profile
    check is a READ gate and cannot be a write gate. Verified 2026-09-07:
    deleting the user_id predicate left all 19 tests in this file and
    test_api_calls_hide.py green, while a second user's PUT returned 200 and
    overwrote the victim's words. The neighbouring unhide/unsave endpoints say
    the same thing about themselves in their own docstrings.

    The profile_id predicate is asserted here too, for the same reason: it is
    unpinned on its own, and one user with two profiles is the ordinary case.
    """
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "MINE"}
        )

    # A colleague who can READ the same org-visible profile, with their own
    # decision on the same (profile, call).
    from fastapi.testclient import TestClient

    from grant_lab.auth.session_cookie import SessionSigner
    from grant_lab.db.models import User

    with pg_session_factory() as s:
        other = User(status="active", email="colleague@umu.se")
        s.add(other)
        s.commit()
        oid = other.id
    signer: SessionSigner = app.state.session_signer
    colleague = TestClient(app)
    colleague.cookies.set("session", signer.make(user_id=oid))
    with colleague:
        assert (
            colleague.post(f"/api/calls/{cid}/hide", params={"profile_id": pid}).status_code
            == 200
        ), "the colleague must be able to READ the profile, or this gate is vacuous"
        r = colleague.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "THEIRS"}
        )
    assert r.status_code == 200

    # Two rows, each keeping its own author's words. One row, or one text on
    # both, means the write crossed a user.
    with pg_session_factory() as s:
        by_user = {
            d.user_id: d.note
            for d in s.execute(select(CallDecision).where(CallDecision.call_id == cid))
            .scalars()
            .all()
        }
    assert by_user == {uid: "MINE", oid: "THEIRS"}


def test_a_note_cannot_cross_to_another_profile(monkeypatch, pg_url, pg_session_factory):
    """The profile half, alone. One user, two of their OWN profiles, one call
    hidden on both: a note written against profile A must not land on B."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid_a = _login(app, pg_session_factory)
    from grant_lab.db.models import Profile, ProfileKind

    with pg_session_factory() as s:
        p = Profile(
            name="Grp B",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="d",
            structured={},
            created_by=uid,
        )
        s.add(p)
        s.commit()
        pid_b = p.id
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid_a})
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid_b})
        assert (
            client.put(
                f"/api/calls/{cid}/note",
                params={"profile_id": pid_a},
                json={"note": "A ONLY"},
            ).status_code
            == 200
        )
    with pg_session_factory() as s:
        by_profile = {
            d.profile_id: d.note
            for d in s.execute(select(CallDecision).where(CallDecision.call_id == cid))
            .scalars()
            .all()
        }
    assert by_profile == {pid_a: "A ONLY", pid_b: None}


def test_a_stale_expected_state_is_a_409_and_writes_nothing(
    monkeypatch, pg_url, pg_session_factory
):
    """One user, two tabs. Tab A hides and starts typing; tab B saves the same
    call, which clears the note (D9). Tab A's write must NOT land: a reason
    written about hiding, filed against a save, is the exact misfiling D9
    exists to prevent, and (call_id, profile_id) cannot tell the two apart."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "wrong field", "expected_state": "hidden"},
        )
    assert r.status_code == 409
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert (row.state, row.note) == ("saved", None)


def test_without_expected_state_the_write_is_unconditional(
    monkeypatch, pg_url, pg_session_factory
):
    """The field is optional and the 409 arm must not fire without it, or every
    caller that does not track state gets a 409 it cannot act on."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        r = client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "ok"}
        )
    assert r.status_code == 200


def test_note_requires_auth(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    from fastapi.testclient import TestClient

    with TestClient(app) as anon:
        r = anon.put(f"/api/calls/{cid}/note", params={"profile_id": 1}, json={"note": "x"})
    assert r.status_code == 401


def test_hiding_a_saved_call_clears_its_note(monkeypatch, pg_url, pg_session_factory):
    """G5, D9. Today the upserts leave `note` out of their set_, so a note
    written while saving survives into the hide. The code already calls a flip
    a NEW decision -- it moves decided_at and decided_by for that reason -- so
    carrying the old decision's reasoning into it is wrong, and D7 would then
    date that note by the new decision."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "ask Anna before the deadline"},
        )
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert (row.state, row.note) == ("hidden", None)


def test_saving_a_hidden_call_clears_its_note(monkeypatch, pg_url, pg_session_factory):
    """The other direction, written separately because the two set_ blocks are
    separate code and either can be got wrong alone. (Parametrized cases are
    independently identifiable too, so parametrizing would NOT have hidden the
    split -- an earlier draft of this plan claimed it would, and that was
    wrong.)"""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "budget far too small"},
        )
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    assert (row.state, row.note) == ("saved", None)


def test_re_hiding_an_already_hidden_call_keeps_the_note(
    monkeypatch, pg_url, pg_session_factory
):
    """D9 clears the note on a FLIP, not on every upsert. `h` is a keyboard key
    and people press it twice; an unconditional note=None in the set_ would
    silently destroy the reason they had just typed. The CASE expression is
    what makes hidden -> hidden a no-op for the note."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "wrong field"},
        )
        before = _decided_at(pg_session_factory, uid)
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    # decided_at FIRST. Without it the gate cannot tell "the CASE spared the
    # note" from "the upsert wrote nothing at all" -- and the second reading is
    # the likelier regression, because on_conflict_do_NOTHING also returns 200
    # and also leaves the note standing. Verified 2026-09-07: with hide_call
    # regressed to do_nothing, a status-code assertion stays green and only the
    # decided_at assertion below reddens. `now()` is transaction_timestamp, and
    # the two hides are separate requests, so a real DO UPDATE must move it.
    assert row.decided_at > before, (
        "the second hide must actually UPDATE the row, or this test proves nothing"
    )
    assert row.note == "wrong field"


def test_re_saving_an_already_saved_call_keeps_the_note(
    monkeypatch, pg_url, pg_session_factory
):
    """The save-side mirror, and it is NOT decorative.

    hide_call has both a flip gate and this non-flip gate; save_call had only
    the flip gate. Verified 2026-09-07: replacing save_call's case(...) with a
    bare `"note": None` -- the obvious, wrong-looking-right version -- left the
    ENTIRE suite green at 5498 passed. `s` is a keyboard key exactly as `h` is,
    so that mutant destroys a reason the user just typed, in production, with
    nothing in CI able to see it."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "matches Anna's ERC angle"},
        )
        before = _decided_at(pg_session_factory, uid)
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
    with pg_session_factory() as s:
        row = s.execute(
            select(CallDecision).where(CallDecision.user_id == uid)
        ).scalar_one()
    # See the hide-side mirror above for why decided_at is asserted first.
    assert row.decided_at > before, (
        "the second save must actually UPDATE the row, or this test proves nothing"
    )
    assert (row.state, row.note) == ("saved", "matches Anna's ERC angle")


def test_unhiding_destroys_the_note(monkeypatch, pg_url, pg_session_factory):
    """G3, D4. Undecided is the ABSENCE of a row, so the note dies with the
    decision, and re-hiding starts blank.

    NOTHING IS WRITTEN AFTER THE UNHIDE. An earlier draft of this gate sent
    PUT {"note": null} and then asserted null -- the endpoint echoing its own
    input. It passed with the DELETE replaced by a no-op."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "no"}
        )
        client.delete(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    # The ROW is gone -- that IS the guarantee, and it is read, never written.
    with pg_session_factory() as s:
        assert s.execute(select(CallDecision)).scalars().all() == []
    # Re-hide and INSPECT. No write between the unhide and the assertion.
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
    with pg_session_factory() as s:
        row = s.execute(select(CallDecision)).scalar_one()
    assert row.note is None
