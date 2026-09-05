"""Migration 0074: `boards.min_score` / `boards.items_per_page` /
`boards.page_seconds` CHECK constraints.

`compare_metadata` does not compare CHECK bodies (see
test_migration_0072_boards.py's own module docstring), so this asserts against
the live database, not just the model. Round 2's review widened
`ck_boards_items_per_page` -- originally created in migration 0072 as
`items_per_page > 0`, with no ceiling -- into a range, and this migration
carries that widening (rather than an 0075), alongside the min_score and
page_seconds ranges it already had. All three exist to stop a value like 500
`items_per_page` or 100000 `page_seconds` from persisting silently through
`PUT /api/boards/{id}`, and a value like 3_000_000_000 from clearing int4's
range and surfacing as an uncaught 500 (web/api/boards.py's own pydantic
bounds are the first line of defence; this is the DB floor those bounds
mirror).

Every test here uses `pg_session_factory` and reads the already-migrated
shared container; none drives alembic itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _insert_board(s, *, slug: str, pid: int, uid: int, **overrides):
    cols = {
        "slug": slug,
        "title": "t",
        "profile_id": pid,
        "orientation": "landscape",
        "created_by": uid,
    }
    cols.update(overrides)
    names = ", ".join(cols)
    placeholders = ", ".join(f":{k}" for k in cols)
    return s.scalar(
        text(f"INSERT INTO boards ({names}) VALUES ({placeholders}) RETURNING id"),
        cols,
    )


# ---------------------------------------------------------------------------
# min_score: BETWEEN 0 AND 100
# ---------------------------------------------------------------------------
def test_min_score_check_rejects_below_zero(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 min_score low")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        _insert_board(s, slug="a" * 16, pid=pid, uid=uid, min_score=-1)
        s.commit()


def test_min_score_check_rejects_above_hundred(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 min_score high")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        _insert_board(s, slug="b" * 16, pid=pid, uid=uid, min_score=101)
        s.commit()


def test_min_score_check_accepts_boundaries(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 min_score boundaries")
    with pg_session_factory() as s:
        _insert_board(s, slug="c" * 16, pid=pid, uid=uid, min_score=0)
        _insert_board(s, slug="d" * 16, pid=pid, uid=uid, min_score=100)
        s.commit()


# ---------------------------------------------------------------------------
# page_seconds: BETWEEN 1 AND 3600
# ---------------------------------------------------------------------------
def test_page_seconds_check_rejects_zero(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 page_seconds zero")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        _insert_board(s, slug="e" * 16, pid=pid, uid=uid, page_seconds=0)
        s.commit()


def test_page_seconds_check_rejects_above_3600(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 page_seconds high")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        _insert_board(s, slug="f" * 16, pid=pid, uid=uid, page_seconds=3601)
        s.commit()


def test_page_seconds_check_accepts_boundaries(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 page_seconds boundaries")
    with pg_session_factory() as s:
        _insert_board(s, slug="g" * 16, pid=pid, uid=uid, page_seconds=1)
        _insert_board(s, slug="h" * 16, pid=pid, uid=uid, page_seconds=3600)
        s.commit()


# ---------------------------------------------------------------------------
# items_per_page: BETWEEN 1 AND 64 -- originally `> 0` in migration 0072;
# this file drops and recreates it as a range (see module docstring).
# ---------------------------------------------------------------------------
def test_items_per_page_check_rejects_zero(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 items_per_page zero")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        _insert_board(s, slug="i" * 16, pid=pid, uid=uid, items_per_page=0)
        s.commit()


def test_items_per_page_check_rejects_above_64(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 items_per_page high")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        _insert_board(s, slug="j" * 16, pid=pid, uid=uid, items_per_page=65)
        s.commit()


def test_items_per_page_check_accepts_boundaries(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="0074 items_per_page boundaries")
    with pg_session_factory() as s:
        _insert_board(s, slug="k" * 16, pid=pid, uid=uid, items_per_page=1)
        _insert_board(s, slug="l" * 16, pid=pid, uid=uid, items_per_page=64)
        s.commit()
