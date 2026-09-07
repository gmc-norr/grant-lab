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
