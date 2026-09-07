"""The 05:00 job: what it publishes, what it refuses to publish, what it
re-decides under the lock, and what it does not hold while it works."""

from __future__ import annotations

from sqlalchemy import select, text

from grant_lab.boards import auto_publish as ap
from grant_lab.boards.auto_publish import reconcile_auto_boards
from grant_lab.db.models import BoardItem, BoardSuppression
from tests.test_board_auto_selection import _ranked_call
from tests.test_boards_qr_at_approval import _make_board


def _sql(pg_session_factory, board_id, sql, **kw):
    """One-statement UPDATE against `boards`, committed. Used both to arm a
    board and, from inside the `qr_for` seam, to simulate a settings request
    landing BETWEEN the two passes."""
    with pg_session_factory() as s:
        s.execute(text(sql), {"b": board_id, **kw})
        s.commit()


def _set_auto(pg_session_factory, board_id, on=True):
    _sql(pg_session_factory, board_id, "UPDATE boards SET auto_publish = :v WHERE id = :b", v=on)


def test_a_manual_board_is_untouched(pg_session_factory, profile_factory):
    """GATE 1 -- the control. Every board that exists when this ships is manual,
    and this job must be invisible to all of them.

    NOTE what this does NOT cover: the board here was never auto, so it never
    enters `board_ids` and never reaches pass 2. Pass 2's own `auto_publish`
    re-check is invisible to it. That is gate 6's job.
    """
    pid, owner = profile_factory(name="job manual")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="m" * 16)
    _ranked_call(pg_session_factory, external_id="job-m1", profile_id=pid, score=90)
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.scalars(select(BoardItem).where(BoardItem.board_id == bid)).all() == []


def test_an_auto_board_publishes_qualifying_calls(pg_session_factory, profile_factory):
    """GATE 2. And the row records that no human approved it."""
    pid, owner = profile_factory(name="job auto")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="w" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-a1", profile_id=pid, score=90)
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        item = s.get(BoardItem, (bid, cid))
        assert item is not None
        assert item.approved_by is None, "an auto row must not claim a human approver"
        assert item.qr_modules > 0, "the QR was not rendered"


def test_a_suppressed_call_is_not_republished(pg_session_factory, profile_factory):
    """GATE 3 -- the load-bearing one. This is what makes a takedown a takedown."""
    pid, owner = profile_factory(name="job suppressed")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="x" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-s1", profile_id=pid, score=95)
    with pg_session_factory() as s:
        s.add(BoardSuppression(board_id=bid, call_id=cid, suppressed_by=owner))
        s.commit()
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is None, "the 05:00 job undid a takedown"


def test_a_call_that_stops_qualifying_leaves(pg_session_factory, profile_factory):
    """GATE 4. The board is the current best, not an accumulating pile.

    SCOPE, stated because the first draft's mutation table claimed more: this
    covers TWO SEQUENTIAL RUNS CONVERGING. It does NOT cover a settings change
    landing between the two passes of ONE run -- the second call's pass 1
    already reads the new `min_score`, so pass 2 recomputing changes nothing
    here. Gate 7 covers that, and only gate 7 can.
    """
    pid, owner = profile_factory(name="job leaves")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="y" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-l1", profile_id=pid, score=90)
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is not None
    _sql(pg_session_factory, bid, "UPDATE boards SET min_score = 95 WHERE id = :b")
    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is None


def test_the_encode_does_not_run_under_the_board_lock(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 5. Asserting the QR is CORRECT passes with the encode inside the
    write transaction, which is the defect. `FOR UPDATE NOWAIT` from a second
    connection answers 'was the row locked' without a timeout. Same shape as
    tests/test_boards_qr_at_approval.py's gate 12."""
    from sqlalchemy.exc import DBAPIError, OperationalError

    pid, owner = profile_factory(name="job lock")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="z" * 16)
    _set_auto(pg_session_factory, bid)
    for i in range(3):
        _ranked_call(pg_session_factory, external_id=f"job-k{i}", profile_id=pid, score=90 - i)

    held: list[str] = []
    real = ap.qr_for

    def probing(url):
        try:
            with pg_session_factory() as probe:
                probe.execute(
                    text("SELECT id FROM boards WHERE id = :b FOR UPDATE NOWAIT"), {"b": bid}
                )
        except (OperationalError, DBAPIError):
            held.append("board")
        return real(url)

    monkeypatch.setattr(ap, "qr_for", probing)
    reconcile_auto_boards(session_factory=pg_session_factory)

    with pg_session_factory() as s:
        stored = s.scalars(select(BoardItem.qr_modules).where(BoardItem.board_id == bid)).all()
    assert stored and all(m > 0 for m in stored), f"nothing was published: {stored}"
    assert not held, f"the encode ran under the board lock ({len(held)} of 3)"


def test_auto_publish_turned_off_between_the_passes_publishes_nothing(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 6. Turning automation off must FREEZE the board -- the spec's own
    promise. This is the ONLY test that reaches pass 2's `auto_publish`
    re-check: gate 1's board was never auto, so it never enters `board_ids`.

    The seam is `qr_for`. It runs after pass 1 and before pass 2 with NO lock
    held, which is exactly where a concurrent settings request would land.
    """
    pid, owner = profile_factory(name="job flip off midway")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="R" * 16)
    _set_auto(pg_session_factory, bid)
    _ranked_call(pg_session_factory, external_id="job-f1", profile_id=pid, score=90)

    real = ap.qr_for

    def flip(url):
        _sql(pg_session_factory, bid, "UPDATE boards SET auto_publish = false WHERE id = :b")
        return real(url)

    monkeypatch.setattr(ap, "qr_for", flip)
    reconcile_auto_boards(session_factory=pg_session_factory)

    with pg_session_factory() as s:
        rows = s.scalars(select(BoardItem).where(BoardItem.board_id == bid)).all()
    assert rows == [], "the job published to a board whose automation was switched off"


def test_min_score_raised_between_the_passes_is_honoured(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 7. Pass 1's selection is ADVISORY. This is the ONLY test that can
    tell `board_selection` recomputed under the lock from `likely` being reused,
    because it is the only one where the settings move BETWEEN the passes.
    """
    pid, owner = profile_factory(name="job raise min_score midway")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="S" * 16)
    _set_auto(pg_session_factory, bid)
    _ranked_call(pg_session_factory, external_id="job-f2", profile_id=pid, score=90)

    real = ap.qr_for

    def raise_floor(url):
        _sql(pg_session_factory, bid, "UPDATE boards SET min_score = 95 WHERE id = :b")
        return real(url)

    monkeypatch.setattr(ap, "qr_for", raise_floor)
    reconcile_auto_boards(session_factory=pg_session_factory)

    with pg_session_factory() as s:
        rows = s.scalars(select(BoardItem).where(BoardItem.board_id == bid)).all()
    assert rows == [], "pass 2 wrote pass 1's stale selection instead of re-deciding"


def test_one_boards_failure_does_not_cost_the_others(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 8. A nightly job that stops at the first bad board is the
    'silently stops updating' failure this feature exists to prevent -- and
    APScheduler swallows the traceback, so it would be invisible."""
    pid, owner = profile_factory(name="job isolation")
    good = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="T" * 16)
    bad = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="U" * 16)
    _set_auto(pg_session_factory, good)
    _set_auto(pg_session_factory, bad)
    _ranked_call(pg_session_factory, external_id="job-i1", profile_id=pid, score=90)

    real = ap.qr_for
    seen: list[int] = []

    def blow_up_once(url):
        # Fail the FIRST board the job reaches, whichever that is, then behave.
        if not seen:
            seen.append(1)
            raise RuntimeError("qr backend down")
        return real(url)

    monkeypatch.setattr(ap, "qr_for", blow_up_once)
    summary = reconcile_auto_boards(session_factory=pg_session_factory)

    assert summary.boards_failed == 1, f"the failure was not isolated: {summary}"
    with pg_session_factory() as s:
        rows = s.scalars(select(BoardItem).where(BoardItem.board_id.in_([good, bad]))).all()
    assert rows, "the second board got nothing -- the first board's failure aborted the run"
