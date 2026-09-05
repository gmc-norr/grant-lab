"""The board schema, as §4.3 fixes it plus D7's stored orientation.

FIXTURES. `pg_session_factory` (tests/conftest.py:151) is a FACTORY --
`with pg_session_factory() as s:`. There is no `db_session` in this repo.
`profile_factory` (:184) returns a `(profile_id, user_id)` tuple.
"""

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import Board, BoardItem, Subscription


def test_board_columns_match_the_spec():
    cols = {c.name for c in inspect(Board).columns}
    assert cols == {
        "id", "slug", "title", "profile_id", "is_published", "min_score",
        "orientation", "items_per_page", "max_pages", "page_seconds",
        "created_by", "published_by", "published_at", "created_at", "updated_at",
    }


def test_board_items_primary_key_is_board_and_call():
    pk = {c.name for c in inspect(BoardItem).primary_key}
    assert pk == {"board_id", "call_id"}


def test_orientation_is_constrained_to_two_values(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile A")
    with pg_session_factory() as s:
        s.add(Board(slug="x" * 16, title="t", profile_id=pid,
                    orientation="diagonal", created_by=uid))
        with pytest.raises(IntegrityError):
            s.flush()


def test_slug_is_unique(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile B")
    with pg_session_factory() as s:
        for _ in range(2):
            s.add(Board(slug="s" * 16, title="t", profile_id=pid,
                        orientation="landscape", created_by=uid))
        with pytest.raises(IntegrityError):
            s.flush()


def test_a_slug_under_16_chars_is_refused(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Board profile C")
    with pg_session_factory() as s:
        s.add(Board(slug="s" * 15, title="t", profile_id=pid,
                    orientation="landscape", created_by=uid))
        with pytest.raises(IntegrityError):
            s.flush()


def test_subscription_carries_its_own_board_alert_flag():
    cols = {c.name for c in inspect(Subscription).columns}
    assert "board_alerts_enabled" in cols
    assert "digest_enabled" in cols, "the board flag is ADDED, never a rename"
