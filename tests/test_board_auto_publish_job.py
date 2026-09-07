"""The 05:00 job: what it publishes, what it refuses to publish, what it
re-decides under the lock, and what it does not hold while it works."""

from __future__ import annotations

from sqlalchemy import select, text

from grant_lab.boards import auto_publish as ap
from grant_lab.boards.auto_publish import reconcile_auto_boards
from grant_lab.boards.public import public_board_items
from grant_lab.boards.qr import qr_for
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
    summary = reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        assert s.get(BoardItem, (bid, cid)) is None
    # The counters feed the tick's operator log and were pinned by nothing.
    assert summary.rows_removed == 1, f"the departure was not counted: {summary}"


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
    # Distinct scores, best first: `cids[0]` must end up at position 0.
    cids = [
        _ranked_call(pg_session_factory, external_id=f"job-k{i}", profile_id=pid, score=90 - i)
        for i in range(3)
    ]

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
        rows = s.execute(
            select(BoardItem.position, BoardItem.call_id, BoardItem.qr_modules)
            .where(BoardItem.board_id == bid)
            .order_by(BoardItem.position)
        ).all()
    # MINOR 5: `stored and ...` was the whole vacuity guard, so a run that
    # published 1 of 3 passed while the message below still said "of 3". The
    # COUNT is part of the guard.
    assert len(rows) == 3 and all(m > 0 for _p, _c, m in rows), f"nothing was published: {rows}"
    # The wall reads `position` -- that is what `ix_board_items_board_position`
    # is for. Reversing this order puts the WORST-scoring call first on a
    # corridor screen, and every other assertion in this module survives it.
    assert [c for _p, c, _m in rows] == cids, (
        f"cards are not in score order: got call_ids {[c for _p, c, _m in rows]}, "
        f"best-first is {cids}"
    )
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
    # The counters were pinned by NOTHING before this line; `boards_failed`
    # alone let a regression that zeroed them ship green.
    assert summary.rows_added == 1, f"the surviving board's row was not counted: {summary}"
    with pg_session_factory() as s:
        rows = s.scalars(select(BoardItem).where(BoardItem.board_id.in_([good, bad]))).all()
    assert rows, "the second board got nothing -- the first board's failure aborted the run"


def test_pass_2_decides_under_the_board_row_lock(pg_session_factory, profile_factory, monkeypatch):
    """GATE 9 -- the invariant this whole function is built on, which a review
    found pinned by nothing: deleting `with_for_update=True` at the top of pass
    2 reddened NONE of the eight gates. Gate 5's `held` stays empty (it asserts
    the lock is ABSENT during the encode, so losing it makes gate 5 *more*
    green); gates 6 and 7 move their setting strictly BETWEEN the passes, where
    an unlocked `session.get` re-reads the new value anyway and both still pass;
    gates 1-4 and 8 never touch it.

    What is lost without it: a settings PUT that commits DURING pass 2 -- after
    the re-check, before the commit -- writes items onto a board whose
    automation was just switched off. That is the promise `update_board`'s own
    `_lock_board_row` exists to keep.

    Shape. `board_selection` is the one seam called in BOTH passes, so the same
    second-connection `FOR UPDATE NOWAIT` probe gate 5 uses can answer "is the
    board row locked right now" at two points: it must be free in pass 1 (the
    encode runs after it) and held in pass 2. NOWAIT means a missing lock FAILS
    an assertion immediately instead of blocking into pytest-timeout's 120s and
    surfacing as a hang.
    """
    from sqlalchemy.exc import DBAPIError, OperationalError

    pid, owner = profile_factory(name="job pass2 lock")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="V" * 16)
    _set_auto(pg_session_factory, bid)
    _ranked_call(pg_session_factory, external_id="job-p1", profile_id=pid, score=90)

    real_selection = ap.board_selection
    observed: list[bool] = []

    def probing(session, *, board):
        try:
            with pg_session_factory() as probe:
                probe.execute(
                    text("SELECT id FROM boards WHERE id = :b FOR UPDATE NOWAIT"), {"b": bid}
                )
            observed.append(False)
        except (OperationalError, DBAPIError):
            observed.append(True)
        return real_selection(session, board=board)

    monkeypatch.setattr(ap, "board_selection", probing)
    reconcile_auto_boards(session_factory=pg_session_factory)

    # Vacuity first: a job that never reaches pass 2 observes nothing, and the
    # lock assertion below would then be true of an empty run.
    assert len(observed) == 2, f"the job did not run both passes over this board: {observed}"
    with pg_session_factory() as s:
        assert s.scalars(select(BoardItem).where(BoardItem.board_id == bid)).all(), (
            "nothing was published -- the lock assertions here would be vacuous"
        )
    assert observed[0] is False, (
        "pass 1 already held the board row lock -- the encode runs after this "
        "point, so gate 5's promise is gone"
    )
    assert observed[1] is True, (
        "pass 2 decided WITHOUT the board row lock: a settings PUT committing "
        "between the re-check and the commit would be silently overwritten"
    )


def test_a_pass_2_failure_does_not_cost_the_other_boards(
    pg_session_factory, profile_factory, monkeypatch
):
    """GATE 10. Gate 8 injects through `qr_for`, which the job calls in PASS 1 --
    before the lock is taken and before any write. An implementation that
    narrowed the per-board `try:` to cover only the pass-1 block would pass gate
    8 green while leaving every pass-2 failure fatal to the whole night's
    remaining boards, which is the same hole gates 6-8 exist to close.

    This raises from INSIDE pass 2: under the lock, inside the write
    transaction, where gate 8's injection never reaches.

    It also pins `boards_seen` against the counter fix. Incremented inside the
    transaction -- as it was -- the board whose work rolled back still counted
    itself as reconciled, and the tick logged it to an operator.
    """
    pid, owner = profile_factory(name="job pass2 isolation")
    for slug in ("W" * 16, "X" * 16):
        _set_auto(
            pg_session_factory,
            _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug=slug),
        )
    _ranked_call(pg_session_factory, external_id="job-x1", profile_id=pid, score=90)

    real_selection = ap.board_selection
    seen: list[int] = []

    def blow_up_in_pass_2(session, *, board):
        seen.append(board.id)
        # The calls are [B1 pass 1, B1 pass 2, B2 pass 1, B2 pass 2]. The
        # SECOND is the first board's pass 2 -- whichever board that is.
        if len(seen) == 2:
            raise RuntimeError("selection blew up under the board lock")
        return real_selection(session, board=board)

    monkeypatch.setattr(ap, "board_selection", blow_up_in_pass_2)
    summary = reconcile_auto_boards(session_factory=pg_session_factory)

    assert summary.boards_failed == 1, f"a pass-2 failure was not isolated: {summary}"
    assert len(seen) == 4, f"the second board never ran its passes: {seen}"
    failed_board, surviving_board = seen[0], seen[2]
    assert failed_board != surviving_board, f"both passes ran on one board: {seen}"
    with pg_session_factory() as s:
        assert s.scalars(select(BoardItem).where(BoardItem.board_id == surviving_board)).all(), (
            "the second board got nothing -- a pass-2 failure aborted the run"
        )
        assert s.scalars(select(BoardItem).where(BoardItem.board_id == failed_board)).all() == [], (
            # NOT rollback evidence, and this message used to claim it was.
            # `blow_up_in_pass_2` raises INSIDE `board_selection`, before the
            # add/delete loop below it has run at all, so there is nothing for
            # a rollback to undo and this assertion cannot fail. It pins the
            # weaker, still-worth-pinning fact that a board whose pass 2 raised
            # ends the night with no rows. The rollback evidence is
            # `boards_seen == 1` on the assertion below.
            "the board that raised in pass 2 somehow ended the night with rows"
        )
    assert summary.boards_seen == 1, f"the rolled-back board was counted as reconciled: {summary}"
    assert summary.rows_added == 1, f"rows_added counts work that was rolled back: {summary}"


def test_a_changed_call_url_gets_a_fresh_qr_on_the_next_run(pg_session_factory, profile_factory):
    """GATE 12 -- the branch review's IMPORTANT 2.

    `public_board_items` serves the stored QR only while `qr_source_url ==
    call.url`. When a call's url changes after its row was written -- an
    ordinary ingestion outcome -- the card goes out with `qr_modules=0` from
    that moment on. `BoardItem`'s docstring names the remedy as "an admin
    removes the item and re-approves it"; on an AUTO board there is no admin
    in that loop and the call keeps qualifying, so it is never removed and
    the QR never comes back. The wall's primary artefact dies silently.

    The middle third of this test is the part that makes it a gate and not a
    tautology: it asserts the public route really is serving nothing BEFORE
    the second run, so the final assertion is a repair and not a value that
    was there all along."""
    pid, owner = profile_factory(name="job qr refresh")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="r" * 16)
    _set_auto(pg_session_factory, bid)
    cid = _ranked_call(pg_session_factory, external_id="job-q1", profile_id=pid, score=90)

    reconcile_auto_boards(session_factory=pg_session_factory)
    with pg_session_factory() as s:
        item = s.get(BoardItem, (bid, cid))
        stale_source, stale_path = item.qr_source_url, item.qr_path
        assert item.qr_modules > 0, "the first run did not render a QR at all"

    new_url = "https://funder.example/moved/" + "z" * 40
    assert new_url != stale_source
    with pg_session_factory() as s:
        s.execute(text("UPDATE calls SET url = :u WHERE id = :c"), {"u": new_url, "c": cid})
        s.commit()

    # The damage, before the fix has a chance to run: the card is already
    # going out with no QR.
    with pg_session_factory() as s:
        (row,) = public_board_items(s, board_id=bid)
        assert row.url == new_url
        assert row.qr_modules == 0, "the stale-QR precondition this gate needs did not hold"
        assert row.qr_path == ""

    reconcile_auto_boards(session_factory=pg_session_factory)

    expected = qr_for(new_url)
    assert expected.modules > 0
    with pg_session_factory() as s:
        item = s.get(BoardItem, (bid, cid))
        assert item.qr_source_url == new_url, "the row still points at the old url"
        assert item.qr_modules == expected.modules
        assert item.qr_path == expected.path
        assert item.qr_path != stale_path
        (row,) = public_board_items(s, board_id=bid)
        assert row.qr_modules == expected.modules, "the public route is still serving no QR"
        assert row.qr_path == expected.path


def _health(pg_session_factory, board_id):
    from grant_lab.db.models import Board

    with pg_session_factory() as s:
        b = s.get(Board, board_id)
        return (b.last_reconcile_attempt_at, b.last_reconciled_at, b.last_reconcile_error)


def test_a_successful_run_records_that_it_succeeded(pg_session_factory, profile_factory):
    """A corridor wall that quietly stopped updating looks exactly like one that
    is working -- the cards are still there, they are just yesterday's. Nothing
    recorded when the job ran, so nobody could tell.

    Both timestamps, because a run that changes nothing is still a healthy run:
    counting rows would report a board that is fine as a board that is dead.
    """
    pid, owner = profile_factory(name="health ok")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="V" * 16)
    _set_auto(pg_session_factory, bid)
    _ranked_call(pg_session_factory, external_id="health-1", profile_id=pid, score=90)

    assert _health(pg_session_factory, bid) == (None, None, None), "seeded dirty"
    reconcile_auto_boards(session_factory=pg_session_factory)

    attempt, success, error = _health(pg_session_factory, bid)
    assert attempt is not None, "the run recorded no attempt"
    assert success == attempt, "a successful run must stamp both, identically"
    assert error is None


def test_a_FAILING_board_keeps_the_time_it_last_SUCCEEDED(
    pg_session_factory, profile_factory, monkeypatch
):
    """The pair that makes this readable. Overwriting `last_reconciled_at` on a
    failure would say the board was reconciled tonight; leaving it alone says
    'last worked on the 3rd, failing since', which is the sentence an operator
    needs.

    It also proves the stamp is written OUTSIDE the board's own transaction:
    the run below rolls that transaction back, so an outcome recorded inside it
    would be rolled back too -- leaving no trace of exactly the board whose
    health is the question.
    """
    pid, owner = profile_factory(name="health fail")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="W" * 16)
    _set_auto(pg_session_factory, bid)
    _ranked_call(pg_session_factory, external_id="health-2", profile_id=pid, score=90)

    reconcile_auto_boards(session_factory=pg_session_factory)
    _, first_success, _ = _health(pg_session_factory, bid)
    assert first_success is not None

    # A second call arrives, so pass 1 has an encode to make -- and it explodes.
    _ranked_call(pg_session_factory, external_id="health-3", profile_id=pid, score=91)
    monkeypatch.setattr(ap, "qr_for", lambda url: (_ for _ in ()).throw(RuntimeError("boom")))
    summary = reconcile_auto_boards(session_factory=pg_session_factory)
    assert summary.boards_failed == 1, summary

    attempt, success, error = _health(pg_session_factory, bid)
    assert success == first_success, "a failure overwrote the last SUCCESS time"
    assert attempt is not None and attempt > first_success, "the failed attempt was not recorded"
    assert error is not None and "boom" in error, f"the reason was not kept: {error!r}"


def test_a_board_switched_to_manual_between_the_passes_is_not_stamped(
    pg_session_factory, profile_factory, monkeypatch
):
    """Health belongs to boards the job is responsible for. A board whose
    automation was switched off between the passes has no nightly run to be
    late, so stamping it would put a live-looking timestamp on a board nothing
    is watching -- and the skip path reaches the stamp through `continue`,
    which is exactly the flow a plain trailing line would have missed.
    """
    pid, owner = profile_factory(name="health manual")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="X" * 16)
    _set_auto(pg_session_factory, bid)
    _ranked_call(pg_session_factory, external_id="health-4", profile_id=pid, score=90)

    real = ap.qr_for

    def turn_it_off(url):
        # Between the passes: pass 1 encodes, and by pass 2 the board is manual.
        _set_auto(pg_session_factory, bid, on=False)
        return real(url)

    monkeypatch.setattr(ap, "qr_for", turn_it_off)
    reconcile_auto_boards(session_factory=pg_session_factory)

    assert _health(pg_session_factory, bid) == (None, None, None), (
        "a board that is no longer automatic was stamped as if it had a nightly run"
    )
