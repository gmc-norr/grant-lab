"""Taking a card off one board keeps it off, and approving it puts it back."""

from __future__ import annotations

from grant_lab.db.models import BoardItem, BoardSuppression
from tests.test_boards_qr_at_approval import (
    _client_for,
    _make_board,
    _make_call,
    _setup,
)

# NOTE: this file uses `tests.test_boards_qr_at_approval._make_call`, which
# REQUIRES `url=`. The same-named helper in tests/web/test_api_board_items.py
# does NOT take `url` and derives it. They are not interchangeable, and
# importing both into one module is ruff F811 -- see Task 4.


def test_unapproving_records_a_suppression(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 1. Deleting the row is not enough -- the 05:00 job would re-add it."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate1")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="s" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-1", url="https://example.org/s1")

    client = _client_for(app, owner)
    with client:
        approve = client.post(
            f"/api/boards/{board_id}/items", json={"call_ids": [call_id]}
        )
        assert approve.status_code == 200, approve.text
        assert client.delete(f"/api/boards/{board_id}/items/{call_id}").status_code == 204

    with pg_session_factory() as s:
        assert s.get(BoardItem, (board_id, call_id)) is None
        sup = s.get(BoardSuppression, (board_id, call_id))
        assert sup is not None, "the takedown was not recorded"
        assert sup.suppressed_by == owner


def test_approving_clears_the_suppression(monkeypatch, pg_url, pg_session_factory, profile_factory):
    """GATE 2 -- the discriminating control. Without it a call removed once
    could never be re-approved, on any board."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate2")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="t" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-2", url="https://example.org/s2")

    client = _client_for(app, owner)
    with client:
        client.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})
        client.delete(f"/api/boards/{board_id}/items/{call_id}")
        r = client.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        assert s.get(BoardItem, (board_id, call_id)) is not None
        assert s.get(BoardSuppression, (board_id, call_id)) is None, (
            "an explicit re-approval must clear the takedown"
        )


def test_a_suppression_is_scoped_to_one_board(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 3 -- the control that proves this is not archive by another name.
    Two boards on the SAME profile: removing from one leaves the other alone."""
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate3")
    a = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="u" * 16)
    b = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="v" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-3", url="https://example.org/s3")

    client = _client_for(app, owner)
    with client:
        client.post(f"/api/boards/{a}/items", json={"call_ids": [call_id]})
        client.post(f"/api/boards/{b}/items", json={"call_ids": [call_id]})
        client.delete(f"/api/boards/{a}/items/{call_id}")

    with pg_session_factory() as s:
        assert s.get(BoardSuppression, (a, call_id)) is not None
        assert s.get(BoardSuppression, (b, call_id)) is None
        assert s.get(BoardItem, (b, call_id)) is not None, "board B lost a card it never removed"


def test_reapproving_on_one_board_leaves_another_boards_takedown_alone(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 4 -- the one that pins the APPROVE-CLEAR is board-scoped.

    Gate 3 pins that UNAPPROVE is board-scoped. Nothing pins the other half,
    and the two are different code paths. The sequence that discriminates puts
    an approval AFTER a suppression exists on another board -- gate 3 never
    does, so gate 3 cannot see the bug at all.

    The bug, if this is missing: approving a call on board B deletes EVERY
    board's suppression for that call, so an operator's takedown on board A is
    silently undone by an unrelated approval on board B. That is exactly the
    cross-board reach the spec rejects archive for.
    """
    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="suppression gate4")
    a = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="J" * 16)
    b = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="K" * 16)
    call_id = _make_call(pg_session_factory, external_id="sup-4", url="https://example.org/s4")

    client = _client_for(app, owner)
    with client:
        client.post(f"/api/boards/{a}/items", json={"call_ids": [call_id]})
        client.post(f"/api/boards/{b}/items", json={"call_ids": [call_id]})
        assert client.delete(f"/api/boards/{a}/items/{call_id}").status_code == 204
        assert client.delete(f"/api/boards/{b}/items/{call_id}").status_code == 204
        # Re-approve on B ONLY. A's takedown must survive.
        r = client.post(f"/api/boards/{b}/items", json={"call_ids": [call_id]})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        assert s.get(BoardSuppression, (b, call_id)) is None, "B's own re-approval did not clear B"
        assert s.get(BoardSuppression, (a, call_id)) is not None, (
            "approving on board B cleared board A's takedown - the clear is not board-scoped"
        )


def test_a_threshold_sweep_does_not_undo_a_takedown(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 4 -- the one the whole suppression mechanism rests on.

    `POST {"min_score": N}` approves everything at or above N. It is not
    explicit about any particular call, so it must not clear a suppression it
    never named -- the spec's justification for clearing is that an EXPLICIT
    human approval is a newer, stronger signal, and a sweep is not explicit.

    It must also not APPROVE the call. Clearing-but-not-approving and
    approving-but-not-clearing are both wrong: the second leaves an auto board
    showing a card at 09:00 that the 05:00 job removes again, forever.
    """
    from tests.test_board_auto_selection import _ranked_call

    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="sweep vs takedown")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="q" * 16)
    kept = _ranked_call(pg_session_factory, external_id="sweep-keep", profile_id=pid, score=90)
    removed = _ranked_call(pg_session_factory, external_id="sweep-gone", profile_id=pid, score=95)

    client = _client_for(app, owner)
    with client:
        # Put the higher-scoring one up, then take it down deliberately.
        assert client.post(
            f"/api/boards/{board_id}/items", json={"call_ids": [removed]}
        ).status_code == 200
        assert client.delete(f"/api/boards/{board_id}/items/{removed}").status_code == 204
        # Now sweep everything at or above a threshold BELOW both scores.
        r = client.post(f"/api/boards/{board_id}/items", json={"min_score": 80})
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        assert s.get(BoardSuppression, (board_id, removed)) is not None, (
            "the sweep cleared a takedown it never named"
        )
        assert s.get(BoardItem, (board_id, removed)) is None, (
            "the sweep re-approved a suppressed call; the 05:00 job would drop it again"
        )
        # The control: the sweep still does its job for everything else.
        assert s.get(BoardItem, (board_id, kept)) is not None, (
            "the sweep stopped working entirely"
        )


def test_a_suppressed_call_is_not_counted_as_waiting(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 5. A suppressed call is DECIDED, not waiting. Counting it makes the
    'N calls need review' badge name a number that can never go down --
    the same reason already-approved calls drop out of that count."""
    from tests.test_board_auto_selection import _ranked_call

    app = _setup(monkeypatch, pg_url)
    pid, owner = profile_factory(name="sweep waiting count")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="r" * 16)
    call_id = _ranked_call(pg_session_factory, external_id="wait-gone", profile_id=pid, score=91)

    client = _client_for(app, owner)
    with client:
        before = client.get(f"/api/boards/{board_id}/queue").json()["waiting_count"]
        client.post(f"/api/boards/{board_id}/items", json={"call_ids": [call_id]})
        client.delete(f"/api/boards/{board_id}/items/{call_id}")
        after = client.get(f"/api/boards/{board_id}/queue").json()
    assert before == 1, f"expected the call to start out waiting, got {before}"
    assert after["waiting_count"] == 0, "a suppressed call is still counted as waiting"
    row = [r for r in after["items"] if r["call_id"] == call_id][0]
    assert row["suppressed"] is True, "the row does not say WHY it will not go up"
