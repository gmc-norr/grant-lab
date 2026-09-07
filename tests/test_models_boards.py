"""The board schema, as §4.3 fixes it plus D7's stored orientation.

FIXTURES. `pg_session_factory` (tests/conftest.py:151) is a FACTORY --
`with pg_session_factory() as s:`. There is no `db_session` in this repo.
`profile_factory` (:184) returns a `(profile_id, user_id)` tuple.
"""

import re

import pytest
from sqlalchemy import CheckConstraint, inspect
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import (
    ORIENTATION_ITEMS_PER_PAGE,
    Board,
    BoardItem,
    Subscription,
)


def test_board_columns_match_the_spec():
    cols = {c.name for c in inspect(Board).columns}
    assert cols == {
        "id",
        "slug",
        "title",
        "profile_id",
        "is_published",
        "min_score",
        "orientation",
        "items_per_page",
        "max_pages",
        "page_seconds",
        "auto_publish",
        # Board health: what the nightly job last tried, last achieved, and why
        # it failed. Three, because attempt-vs-success is what separates a board
        # that runs fine and changes nothing from one failing every night.
        "last_reconcile_attempt_at",
        "last_reconciled_at",
        "last_reconcile_error",
        "created_by",
        "published_by",
        "published_at",
        "created_at",
        "updated_at",
    }


def test_board_items_primary_key_is_board_and_call():
    pk = {c.name for c in inspect(BoardItem).primary_key}
    assert pk == {"board_id", "call_id"}


def test_orientation_is_constrained_to_two_values(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile A")
    with pg_session_factory() as s:
        s.add(
            Board(slug="x" * 16, title="t", profile_id=pid, orientation="diagonal", created_by=uid)
        )
        with pytest.raises(IntegrityError):
            s.flush()


def test_slug_is_unique(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile B")
    with pg_session_factory() as s:
        for _ in range(2):
            s.add(
                Board(
                    slug="s" * 16,
                    title="t",
                    profile_id=pid,
                    orientation="landscape",
                    created_by=uid,
                )
            )
        with pytest.raises(IntegrityError):
            s.flush()


def test_a_slug_under_16_chars_is_refused(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile C")
    with pg_session_factory() as s:
        s.add(
            Board(slug="s" * 15, title="t", profile_id=pid, orientation="landscape", created_by=uid)
        )
        with pytest.raises(IntegrityError):
            s.flush()


def test_subscription_carries_its_own_board_alert_flag():
    cols = {c.name for c in inspect(Subscription).columns}
    assert "board_alerts_enabled" in cols
    assert "digest_enabled" in cols, "the board flag is ADDED, never a rename"


def test_the_orientation_capacities_agree_with_the_column_defaults():
    """`ORIENTATION_ITEMS_PER_PAGE` and the two column defaults are one fact.

    Tasks 4, 5, 6, 8 and 9 all read this dict to compute the approval cap and
    the D7 flip refusal, but nothing else links it to the DB defaults a board
    is actually born with -- so `landscape: 8` could drift from
    `items_per_page server_default 8` and every one of them would compute a cap
    the row does not have. This ties the two together at the one place both
    are visible.
    """
    default_orientation = Board.__table__.c.orientation.default.arg
    default_capacity = Board.__table__.c.items_per_page.default.arg
    assert ORIENTATION_ITEMS_PER_PAGE[default_orientation] == default_capacity, (
        f"a board created with no overrides gets orientation "
        f"{default_orientation!r} and items_per_page {default_capacity}, but "
        f"ORIENTATION_ITEMS_PER_PAGE says "
        f"{ORIENTATION_ITEMS_PER_PAGE[default_orientation]}"
    )


def test_every_orientation_the_check_allows_has_a_capacity():
    """A third orientation in the CHECK with no dict entry is a KeyError on a wall.

    The CHECK constraint is what the DB will accept; the dict is what the
    renderer can page. If they diverge, a board saves fine and then raises
    when Task 5 computes its cap -- at request time, not at save time.
    """
    check = next(
        c
        for c in Board.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_boards_orientation"
    )
    allowed = set(re.findall(r"'([^']+)'", str(check.sqltext)))
    assert allowed, f"could not parse orientations out of {check.sqltext!r}"
    assert allowed == set(ORIENTATION_ITEMS_PER_PAGE), (
        f"the CHECK accepts {sorted(allowed)} but ORIENTATION_ITEMS_PER_PAGE "
        f"covers {sorted(ORIENTATION_ITEMS_PER_PAGE)}"
    )
