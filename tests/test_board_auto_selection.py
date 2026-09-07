"""What an auto board would publish, before anything writes it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from grant_lab.boards.auto_publish import board_selection
from grant_lab.db.models import Board, BoardSuppression
from tests.test_boards_qr_at_approval import _make_board
from tests.web.test_api_board_items import FUTURE, _make_call, _rank


def _board(pg_session_factory, board_id) -> Board:
    with pg_session_factory() as s:
        return s.get(Board, board_id)


def _ranked_call(pg_session_factory, *, external_id, profile_id, score, deadline=FUTURE):
    """A Call plus its CallRanking against ONE profile.

    The ranking is what `board_candidate_parts` joins on, so a call without one
    never qualifies. The url is derived by `_make_call` as
    https://example.org/<external_id>. `deadline` is threaded through because
    `test_an_expired_call_is_excluded` needs a PAST one and there is no other
    way to get it.
    """
    call_id = _make_call(pg_session_factory, external_id=external_id, deadline=deadline)
    _rank(pg_session_factory, call_id=call_id, profile_id=profile_id, score=score)
    return call_id


def test_the_threshold_is_the_floor(pg_session_factory, profile_factory):
    """A call below min_score never appears, however empty the board is."""
    profile_id, owner_id = profile_factory()
    board_id = _make_board(
        pg_session_factory, profile_id=profile_id, created_by=owner_id, slug="floor-test-board-slug"
    )
    _ranked_call(pg_session_factory, external_id="below-floor", profile_id=profile_id, score=59)

    board = _board(pg_session_factory, board_id)
    with pg_session_factory() as s:
        selection = board_selection(s, board=board)

    assert selection == []


def test_the_capacity_is_the_ceiling(pg_session_factory, profile_factory):
    """items_per_page * max_pages, best score first. With the defaults that is
    8 * 3 = 24."""
    profile_id, owner_id = profile_factory()
    board_id = _make_board(
        pg_session_factory,
        profile_id=profile_id,
        created_by=owner_id,
        slug="ceiling-test-board-slug",
    )
    call_ids = []
    for i in range(30):
        call_id = _ranked_call(
            pg_session_factory,
            external_id=f"cap-{i}",
            profile_id=profile_id,
            score=100 - i,
        )
        call_ids.append(call_id)

    board = _board(pg_session_factory, board_id)
    with pg_session_factory() as s:
        selection = board_selection(s, board=board)

    assert len(selection) == 24
    # The 24 returned are the HIGHEST-scoring: the first 24 calls inserted
    # (scores 100 down to 77), not just any 24 of the 30.
    assert [s.call_id for s in selection] == call_ids[:24]
    assert selection[0].score == 100
    assert selection[-1].score == 77


def test_a_suppressed_call_is_excluded(pg_session_factory, profile_factory):
    """GATE -- this is what makes a takedown stick."""
    profile_id, owner_id = profile_factory()
    board_id = _make_board(
        pg_session_factory,
        profile_id=profile_id,
        created_by=owner_id,
        slug="suppress-test-board-slug",
    )
    call_id = _ranked_call(
        pg_session_factory, external_id="suppressed", profile_id=profile_id, score=90
    )

    with pg_session_factory() as s:
        s.add(BoardSuppression(board_id=board_id, call_id=call_id, suppressed_by=owner_id))
        s.commit()

    board = _board(pg_session_factory, board_id)
    with pg_session_factory() as s:
        selection = board_selection(s, board=board)

    assert selection == []


def test_an_expired_call_is_excluded(pg_session_factory, profile_factory):
    """The deadline filter, inherited from board_candidate_parts."""
    profile_id, owner_id = profile_factory()
    board_id = _make_board(
        pg_session_factory,
        profile_id=profile_id,
        created_by=owner_id,
        slug="expired-test-board-slug",
    )
    past = datetime.now(UTC) - timedelta(days=1)
    _ranked_call(
        pg_session_factory, external_id="expired", profile_id=profile_id, score=90, deadline=past
    )

    board = _board(pg_session_factory, board_id)
    with pg_session_factory() as s:
        selection = board_selection(s, board=board)

    assert selection == []
