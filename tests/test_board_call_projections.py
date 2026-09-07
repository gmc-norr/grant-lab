"""The board queries must not load Call columns their responses never contain.

MEASURED on this tree, 200 rows, best of five:

    board_queue          118.53 ms  ->  21.58 ms
    public_board_items   117.63 ms  ->  14.06 ms

Almost all of it is `embedding`: a 1024-dim vector ships as ~9 kB of TEXT and
pgvector parses it back into 1024 floats in pure Python, per row. `full_text`
and `raw` are simply large.

These gates are BEHAVIOURAL on purpose. Asserting that the source contains
`.options(...)` would pass on a call that hands the options to the wrong
statement and fail on a correct refactor -- neither of which is the property.
What matters is that the columns do not arrive.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError

from grant_lab.boards.public import public_board_items
from grant_lab.boards.queue import board_queue
from grant_lab.db.models import Board, BoardItem
from tests.test_boards_queue import _make_board, _make_call, _rank


def test_a_queue_row_REFUSES_a_column_the_response_never_contains(
    pg_session_factory, profile_factory
):
    """`raiseload=True` is what makes the deferral a contract rather than a
    hope. Without it a consumer that reaches for `full_text` GETS it, via one
    extra SELECT per row -- silently turning a saved ~100 ms into an N+1 that
    nothing measures and no test notices.

    So the gate is that reaching for it RAISES. A future consumer meets the
    same error, which makes widening this a decision someone takes on purpose
    rather than a cliff they fall off.
    """
    pid, owner = profile_factory(name="projection queue")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="P" * 16)
    cid = _make_call(pg_session_factory, external_id="proj-1")
    _rank(pg_session_factory, call_id=cid, profile_id=pid, score=90)

    with pg_session_factory() as s:
        board = s.get(Board, bid)
        rows, _waiting = board_queue(s, board=board, limit=10, offset=0)
        assert rows, "the queue returned nothing -- this gate would pass vacuously"
        for column in ("embedding", "full_text", "raw"):
            with pytest.raises(InvalidRequestError):
                getattr(rows[0].call, column)


def test_the_queue_row_still_carries_everything_the_response_DOES_render(
    pg_session_factory, profile_factory
):
    """Guards the gate above. Deferring every column would make "reaching for
    full_text raises" pass while emptying every card on the screen -- including
    `source`, which is a RELATIONSHIP and so not covered by the column deferral
    at all.
    """
    pid, owner = profile_factory(name="projection control")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="U" * 16)
    cid = _make_call(pg_session_factory, external_id="proj-3")
    _rank(pg_session_factory, call_id=cid, profile_id=pid, score=90)

    with pg_session_factory() as s:
        board = s.get(Board, bid)
        rows, _ = board_queue(s, board=board, limit=10, offset=0)
        call = rows[0].call
        assert call.id == cid
        assert call.title and call.url
        assert call.deadline is not None
        assert call.source is not None and call.source.name


def test_the_public_board_query_does_not_even_ASK_for_those_columns(
    pg_session_factory, profile_factory
):
    """`public_board_items` returns plain dataclasses, so no ORM object survives
    for a deferral to be proved against -- the statement itself is the evidence.

    This is the UNAUTHENTICATED route and the one capped at 192 cards, so it is
    the worst place in the app to ship 192 embeddings nobody reads.
    """
    pid, owner = profile_factory(name="projection public")
    bid = _make_board(pg_session_factory, profile_id=pid, created_by=owner, slug="S" * 16)
    cid = _make_call(pg_session_factory, external_id="proj-2")
    with pg_session_factory() as s:
        s.add(
            BoardItem(
                board_id=bid, call_id=cid, position=0, approved_by=owner,
                qr_path="M0 0h1v1h-1z", qr_modules=25,
                qr_source_url="https://example.org/proj-2",
            )
        )
        s.commit()

    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with pg_session_factory() as s:
        bind = s.get_bind()
        event.listen(bind, "before_cursor_execute", capture)
        try:
            rows = public_board_items(s, board_id=bid)
        finally:
            event.remove(bind, "before_cursor_execute", capture)

    assert rows, "no rows came back -- this gate would pass on an empty SELECT"
    against_calls = [q for q in statements if "FROM calls" in q]
    assert against_calls, f"no query against calls was captured: {statements}"

    # WORD BOUNDARIES, not `in`. A substring test for "calls.embedding" also
    # matches `calls.embedding_model` -- a cheap Text column this query
    # legitimately selects -- and the gate then fails against a correct fix.
    # It cost one debugging round here; the same shape has cost more elsewhere.
    for column in ("embedding", "full_text", "raw"):
        pattern = re.compile(rf"\bcalls\.{column}\b")
        assert not any(pattern.search(q) for q in against_calls), (
            f"calls.{column} is still selected by the public board query"
        )

    # The control: the columns the response DOES render are still asked for, so
    # this cannot pass by matching a query that selects nothing from calls.
    assert any("calls.title" in q for q in against_calls)


def test_there_is_exactly_ONE_copy_of_the_deferred_column_list():
    """Three call sites defer the same three columns for the same reason, and
    three COPIES of that list is how they drift -- silently, because a query
    that stops deferring gets slower and stays correct. Nothing goes red.

    TWO checks, because the first alone is evadable and I proved it: a mutation
    that copied the list back into `dashboard.py` under an aliased import
    (`_d(Call.embedding, raiseload=True)`) sailed past a scan for the literal
    `defer(Call.embedding`. Keying on `raiseload` instead catches any spelling
    of the deferral while still ignoring the two legitimate `Call.embedding`
    FILTERS in admin.py and embeddings/refresh.py, which never mention it.

    What neither check catches: a copy written without `raiseload=True`. That is
    a different defect -- it drops the contract as well as duplicating the list
    -- and it is worth saying that out loud rather than implying this pins more
    than it does.
    """
    import pathlib as _p
    import re as _re

    root = _p.Path(__file__).resolve().parents[1] / "src" / "grant_lab"
    # The FILE, not the line. A line number here rots the first time somebody
    # edits the docstring above the list -- which happened while writing this,
    # in this very test.
    definitions = sorted(
        {
            str(path.relative_to(root))
            for path in root.rglob("*.py")
            for line in path.read_text().splitlines()
            if "Call.embedding" in line and "raiseload" in line
        }
    )
    assert definitions == ["db/projections.py"], (
        f"the deferred-column list has been copied: {definitions}"
    )

    # And the biggest consumer really reaches for the shared one, rather than
    # keeping a private list that merely happens to agree today.
    dashboard = (root / "web" / "dashboard.py").read_text()
    assert _re.search(r"_unrendered\s*=\s*call_columns_no_response_renders\(\)", dashboard), (
        "web/dashboard.py no longer uses the shared list"
    )
