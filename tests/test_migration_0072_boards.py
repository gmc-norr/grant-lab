"""Migration 0072: `boards` + `board_items`.

Two tables Plan C owns end to end (Global Constraints, "Two migrations, not
one"). This file asserts what `test_schema_drift.py` would NOT catch on its
own: a CHECK constraint's actual body (compare_metadata does not compare
CHECK bodies) and the composite PK on `board_items`.

Every test here uses `pg_session_factory` as its session and reads the
already-migrated shared container; none drives alembic itself. Its teardown
TRUNCATE is what keeps a failed INSERT from leaking a row into a later test on
the same xdist worker.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


def _mk_call_via_session(s, external_id: str, slug: str) -> int:
    sid = s.scalar(
        text(
            "INSERT INTO sources (name, slug) VALUES (:n, :s) "
            "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ),
        {"n": f"Fixture Source {slug}", "s": slug},
    )
    return s.scalar(
        text(
            "INSERT INTO calls (source_id, external_id, url, title, keywords, raw) "
            "VALUES (:s, :x, 'http://example.test/c', 'Fixture call 72', '{}', '{}'::jsonb) "
            "RETURNING id"
        ),
        {"s": sid, "x": external_id},
    )


# ---------------------------------------------------------------------------
# Gate 1 - both tables exist at head.
# ---------------------------------------------------------------------------
def test_both_tables_exist_at_head(pg_session_factory):
    with pg_session_factory() as s:
        insp = inspect(s.bind)
        names = insp.get_table_names()
    assert "boards" in names
    assert "board_items" in names


# ---------------------------------------------------------------------------
# Gate 2 - boards.slug has a UNIQUE index/constraint.
# ---------------------------------------------------------------------------
def test_boards_slug_is_unique_in_the_database(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Migration Board Profile A")
    with pg_session_factory() as s:
        s.execute(
            text(
                "INSERT INTO boards (slug, title, profile_id, orientation, created_by) "
                "VALUES (:slug, 't', :p, 'landscape', :u)"
            ),
            {"slug": "m" * 16, "p": pid, "u": uid},
        )
        s.commit()

    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.execute(
            text(
                "INSERT INTO boards (slug, title, profile_id, orientation, created_by) "
                "VALUES (:slug, 't', :p, 'landscape', :u)"
            ),
            {"slug": "m" * 16, "p": pid, "u": uid},
        )
        s.commit()


# ---------------------------------------------------------------------------
# Gate 3 - the orientation CHECK rejects anything but the two named values.
# compare_metadata does not compare CHECK bodies, so this is asserted against
# the live database, not just the model.
# ---------------------------------------------------------------------------
def test_orientation_check_rejects_a_third_value(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Migration Board Profile B")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.execute(
            text(
                "INSERT INTO boards (slug, title, profile_id, orientation, created_by) "
                "VALUES (:slug, 't', :p, 'diagonal', :u)"
            ),
            {"slug": "n" * 16, "p": pid, "u": uid},
        )
        s.commit()


# ---------------------------------------------------------------------------
# Gate 4 - the slug-length CHECK rejects a 15-char slug (>= 16 required).
# ---------------------------------------------------------------------------
def test_slug_length_check_rejects_15_chars(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Migration Board Profile C")
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.execute(
            text(
                "INSERT INTO boards (slug, title, profile_id, orientation, created_by) "
                "VALUES (:slug, 't', :p, 'landscape', :u)"
            ),
            {"slug": "o" * 15, "p": pid, "u": uid},
        )
        s.commit()


# ---------------------------------------------------------------------------
# Gate 5 - board_items has a composite PRIMARY KEY on (board_id, call_id).
# ---------------------------------------------------------------------------
def test_board_items_composite_primary_key_in_the_database(pg_session_factory, profile_factory):
    pid, uid = profile_factory(name="Migration Board Profile D")
    with pg_session_factory() as s:
        board_id = s.scalar(
            text(
                "INSERT INTO boards (slug, title, profile_id, orientation, created_by) "
                "VALUES (:slug, 't', :p, 'landscape', :u) RETURNING id"
            ),
            {"slug": "p" * 16, "p": pid, "u": uid},
        )
        call_id = _mk_call_via_session(s, "call-72-a", "fixture_72a")
        s.execute(
            text(
                "INSERT INTO board_items (board_id, call_id, position, approved_by) "
                "VALUES (:b, :c, 1, :u)"
            ),
            {"b": board_id, "c": call_id, "u": uid},
        )
        s.commit()
        with pytest.raises(IntegrityError):
            s.execute(
                text(
                    "INSERT INTO board_items (board_id, call_id, position, approved_by) "
                    "VALUES (:b, :c, 2, :u)"
                ),
                {"b": board_id, "c": call_id, "u": uid},
            )
            s.commit()

    # A composite PK, not a PK on board_id alone: the SAME board with a
    # DIFFERENT call must be accepted.
    with pg_session_factory() as s:
        board_id = s.scalar(
            text(
                "INSERT INTO boards (slug, title, profile_id, orientation, created_by) "
                "VALUES (:slug, 't', :p, 'landscape', :u) RETURNING id"
            ),
            {"slug": "q" * 16, "p": pid, "u": uid},
        )
        call_a = _mk_call_via_session(s, "call-72-b", "fixture_72b")
        call_b = _mk_call_via_session(s, "call-72-c", "fixture_72c")
        for cid in (call_a, call_b):
            s.execute(
                text(
                    "INSERT INTO board_items (board_id, call_id, position, approved_by) "
                    "VALUES (:b, :c, 1, :u)"
                ),
                {"b": board_id, "c": cid, "u": uid},
            )
        s.commit()
        n = s.scalar(text("SELECT count(*) FROM board_items WHERE board_id = :b"), {"b": board_id})
        assert n == 2, (
            f"board_items' PK is (board_id, call_id), not board_id alone; the same "
            f"board with two different calls must both be storable, got {n}"
        )
