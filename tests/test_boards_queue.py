"""The queue applies the PROFILE OWNER's hides, and nobody else's.

Spec §6 says the same userless query feeds the queue. The 2026-09-05 decisions
note (D2) overrides that: profiles default to visibility='org'
(db/models/profiles.py:121-123), so a userless queue plus "respect everybody's
triage" would let any logged-in user keep a call off a public wall. Keying off
one named person -- the owner -- is the only rule that respects triage at all.

FIXTURES. `pg_session_factory` (tests/conftest.py:151) is a FACTORY --
`with pg_session_factory() as s:`. `profile_factory` (:184) returns a
`(profile_id, user_id)` tuple.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from grant_lab.boards.queue import board_queue
from grant_lab.db.models import Board, BoardItem, Call, CallDecision, CallRanking, Source, User

FUTURE = datetime.now(UTC) + timedelta(days=30)
PAST = datetime.now(UTC) - timedelta(days=1)


def _make_board(pg_session_factory, *, profile_id, created_by, slug, min_score=60):
    with pg_session_factory() as s:
        b = Board(
            slug=slug,
            title="Corridor board",
            profile_id=profile_id,
            created_by=created_by,
            min_score=min_score,
        )
        s.add(b)
        s.commit()
        return b.id


def _make_call(pg_session_factory, *, external_id, deadline=FUTURE):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id=external_id,
            url=f"https://example.org/{external_id}",
            title=f"Call {external_id}",
            deadline=deadline,
        )
        s.add(c)
        s.commit()
        return c.id


def _rank(pg_session_factory, *, call_id, profile_id, score):
    with pg_session_factory() as s:
        s.add(
            CallRanking(
                call_id=call_id,
                profile_id=profile_id,
                aggregate_score=score,
                rationale="r",
                model="gemma4:e4b-q8",
                prompt_version="v2.0",
            )
        )
        s.commit()


def _decide(pg_session_factory, *, user_id, profile_id, call_id, state):
    with pg_session_factory() as s:
        s.add(CallDecision(user_id=user_id, profile_id=profile_id, call_id=call_id, state=state))
        s.commit()


def _approve(pg_session_factory, *, board_id, call_id, approved_by, position=0):
    with pg_session_factory() as s:
        s.add(
            BoardItem(
                board_id=board_id, call_id=call_id, position=position, approved_by=approved_by
            )
        )
        s.commit()


def _new_user(pg_session_factory, email):
    with pg_session_factory() as s:
        u = User(status="active", email=email)
        s.add(u)
        s.commit()
        return u.id


def _run_queue(pg_session_factory, board_id, *, limit=10, offset=0):
    with pg_session_factory() as s:
        board = s.get(Board, board_id)
        return board_queue(s, board=board, limit=limit, offset=offset)


def test_a_call_the_owner_hid_is_absent_from_the_queue(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Owner hides")
    admin_id = _new_user(pg_session_factory, "admin-a@umu.se")
    # The board is built by someone OTHER than the owner (as §4.3 allows), so a
    # mutation that keys off `board.created_by` instead of the owner cannot
    # coincidentally see the same user here as in
    # test_a_call_the_BOARD_CREATOR_hid_is_still_in_the_queue below.
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=admin_id, slug="a" * 16)
    call_id = _make_call(pg_session_factory, external_id="hidden-by-owner")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=80)
    _decide(pg_session_factory, user_id=owner_id, profile_id=pid, call_id=call_id, state="hidden")

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert rows == []
    assert waiting == 0


def test_a_call_a_COLLEAGUE_hid_is_still_in_the_queue(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Colleague hides")
    colleague_id = _new_user(pg_session_factory, "colleague@umu.se")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="b" * 16)
    call_id = _make_call(pg_session_factory, external_id="hidden-by-colleague")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=80)
    _decide(
        pg_session_factory, user_id=colleague_id, profile_id=pid, call_id=call_id, state="hidden"
    )

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert [r.call.id for r in rows] == [call_id]
    assert waiting == 1


def test_a_call_the_BOARD_CREATOR_hid_is_still_in_the_queue(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Admin builds on someone else's profile")
    admin_id = _new_user(pg_session_factory, "admin@umu.se")
    # The admin, not the owner, creates the board -- exactly §4.3's "any editor".
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=admin_id, slug="c" * 16)
    call_id = _make_call(pg_session_factory, external_id="hidden-by-admin")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=80)
    _decide(pg_session_factory, user_id=admin_id, profile_id=pid, call_id=call_id, state="hidden")

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert [r.call.id for r in rows] == [call_id]
    assert waiting == 1


def test_a_hide_on_a_DIFFERENT_profile_does_not_suppress(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Board profile")
    other_pid, _other_owner = profile_factory(name="Other profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="d" * 16)
    call_id = _make_call(pg_session_factory, external_id="hidden-elsewhere")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=80)
    _decide(
        pg_session_factory, user_id=owner_id, profile_id=other_pid, call_id=call_id, state="hidden"
    )

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert [r.call.id for r in rows] == [call_id]
    assert waiting == 1


def test_below_min_score_is_absent(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Score gate")
    board_id = _make_board(
        pg_session_factory, profile_id=pid, created_by=owner_id, slug="e" * 16, min_score=60
    )
    call_id = _make_call(pg_session_factory, external_id="low-score")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=59)

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert rows == []
    assert waiting == 0


def test_a_past_deadline_is_absent(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Deadline gate")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="f" * 16)
    call_id = _make_call(pg_session_factory, external_id="expired", deadline=PAST)
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=80)

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert rows == []
    assert waiting == 0


def test_an_already_approved_call_is_flagged_not_dropped(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Approved stays visible")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="g" * 16)
    call_id = _make_call(pg_session_factory, external_id="already-approved")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=80)
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert [r.call.id for r in rows] == [call_id]
    assert rows[0].already_approved is True
    # It is already up -- it is not part of the WAITING count.
    assert waiting == 0


def test_the_waiting_count_counts_UNAPPROVED_candidates_only(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Mixed queue")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="h" * 16)
    approved_id = _make_call(pg_session_factory, external_id="approved-one")
    waiting_id = _make_call(pg_session_factory, external_id="waiting-one")
    _rank(pg_session_factory, call_id=approved_id, profile_id=pid, score=90)
    _rank(pg_session_factory, call_id=waiting_id, profile_id=pid, score=70)
    _approve(pg_session_factory, board_id=board_id, call_id=approved_id, approved_by=owner_id)

    rows, waiting = _run_queue(pg_session_factory, board_id)

    # A LIST, not a set: these two are ranked 90 and 70, so this is also the
    # only thing in the file that can see `order_by(aggregate_score.desc())`.
    # As a set it discarded the ordering evidence it already had.
    assert [r.call.id for r in rows] == [approved_id, waiting_id]
    assert waiting == 1
    # Minor 2: nothing else asserts the score REACHES the row -- the min_score
    # tests only prove it participates in the filter.
    assert [r.score for r in rows] == [90, 70]


def test_a_ranking_on_a_DIFFERENT_profile_does_not_admit_a_call_that_fails_the_boards_own_score(
    pg_session_factory, profile_factory
):
    # The board's own profile scores this call BELOW min_score; a different
    # profile scores the SAME call ABOVE it. If the ranking join is not scoped
    # to `board.profile_id`, the join fans out (one row per ranking) and
    # `min_score` gets judged against whichever row survives -- here the OTHER
    # profile's passing score would wrongly admit the call.
    pid, owner_id = profile_factory(name="Board's own profile")
    other_pid, _other_owner = profile_factory(name="Other profile")
    board_id = _make_board(
        pg_session_factory, profile_id=pid, created_by=owner_id, slug="j" * 16, min_score=60
    )
    call_id = _make_call(pg_session_factory, external_id="scored-elsewhere")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=40)
    _rank(pg_session_factory, call_id=call_id, profile_id=other_pid, score=90)

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert rows == []
    assert waiting == 0


def test_a_call_the_owner_SAVED_is_still_in_the_queue(pg_session_factory, profile_factory):
    pid, owner_id = profile_factory(name="Saved stays")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="i" * 16)
    call_id = _make_call(pg_session_factory, external_id="saved-by-owner")
    _rank(pg_session_factory, call_id=call_id, profile_id=pid, score=80)
    _decide(pg_session_factory, user_id=owner_id, profile_id=pid, call_id=call_id, state="saved")

    rows, waiting = _run_queue(pg_session_factory, board_id)

    assert [r.call.id for r in rows] == [call_id]
    assert waiting == 1


def test_limit_and_offset_page_the_rows_without_moving_the_waiting_count(
    pg_session_factory, profile_factory
):
    """Minor 3: nothing else calls `_run_queue` with non-default paging.

    Both `.limit(limit)` and `.offset(offset)` could be deleted outright and
    every other test in this file stays green -- each of them fits inside the
    default `limit=10`. The waiting count is asserted alongside on purpose:
    it is deliberately NOT paged, so a `limit` accidentally spliced into the
    count query would show up here as 2 instead of 3.
    """
    pid, owner_id = profile_factory(name="Paged queue")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug="k" * 16)
    ids = {}
    for name, score in (("top", 95), ("mid", 85), ("low", 75)):
        ids[name] = _make_call(pg_session_factory, external_id=f"paged-{name}")
        _rank(pg_session_factory, call_id=ids[name], profile_id=pid, score=score)

    page1, waiting1 = _run_queue(pg_session_factory, board_id, limit=2, offset=0)
    page2, waiting2 = _run_queue(pg_session_factory, board_id, limit=2, offset=2)

    assert [r.call.id for r in page1] == [ids["top"], ids["mid"]]
    assert [r.call.id for r in page2] == [ids["low"]]
    # Unpaged on both pages -- the count answers "how many need review", not
    # "how many are on this page".
    assert waiting1 == 3
    assert waiting2 == 3
