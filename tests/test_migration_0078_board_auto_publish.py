"""Migration 0078: the auto-publish flag, the suppression table, and an
approver that may be absent."""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


_BOARD_INSERT = text(
    "INSERT INTO boards (slug, title, profile_id, created_by, is_published, "
    "orientation, items_per_page, max_pages, page_seconds) "
    "VALUES (:slug, 't', :p, :u, false, 'landscape', 8, 3, 30)"
)


def test_the_flag_defaults_off_for_every_existing_board(pg_session_factory, profile_factory):
    """Nothing that exists today changes behaviour."""
    pid, owner = profile_factory(name="0078 default profile")
    slug = "a" * 16
    with pg_session_factory() as s:
        s.execute(_BOARD_INSERT, {"slug": slug, "p": pid, "u": owner})
        s.commit()
        got = s.scalar(text("SELECT auto_publish FROM boards WHERE slug = :slug"), {"slug": slug})
    assert got is False


def test_approved_by_accepts_null(pg_session_factory, profile_factory):
    """The whole point: an auto-published row has no approver."""
    pid, owner = profile_factory(name="0078 null approver profile")
    slug = "b" * 16
    with pg_session_factory() as s:
        s.execute(_BOARD_INSERT, {"slug": slug, "p": pid, "u": owner})
        bid = s.scalar(text("SELECT id FROM boards WHERE slug = :slug"), {"slug": slug})
        src = s.scalar(text("SELECT id FROM sources WHERE slug = 'vinnova'"))
        s.execute(
            text(
                "INSERT INTO calls (source_id, external_id, url, title, deadline) VALUES "
                "(:s, 'x-0078', 'https://example.org/0078', 't', now() + interval '30 days')"
            ),
            {"s": src},
        )
        cid = s.scalar(text("SELECT id FROM calls WHERE external_id = 'x-0078'"))
        s.execute(
            text(
                "INSERT INTO board_items (board_id, call_id, position, approved_by, qr_modules) "
                "VALUES (:b, :c, 0, NULL, 0)"
            ),
            {"b": bid, "c": cid},
        )
        s.commit()
        got = s.scalar(
            text("SELECT approved_by FROM board_items WHERE board_id = :b"), {"b": bid}
        )
    assert got is None


def test_the_suppression_table_exists_with_a_composite_key(pg_session_factory):
    """A call is suppressed on ONE board, so (board_id, call_id) is the key."""
    with pg_session_factory() as s:
        insp = inspect(s.get_bind())
        assert "board_suppressions" in insp.get_table_names()
        pk = insp.get_pk_constraint("board_suppressions")["constrained_columns"]
    assert sorted(pk) == ["board_id", "call_id"]


def test_0078_round_trip(migrated_engine, pg_url, profile_factory):
    """pg_url is the SHARED session container: this downgrades then upgrades,
    so it MUST leave the schema at head for later tests -- done in `finally`.

    DEVIATION FROM THE BRIEF, measured: the brief's draft read the profile
    with `SELECT id FROM profiles LIMIT 1`, assuming one already exists. It
    does not -- `pg_session_factory` (which every sibling test in this file
    uses via `profile_factory`) TRUNCATEs every table, `profiles` included, in
    its own teardown, so by the time this test runs last in the file the table
    is empty and that SELECT returns NULL, surfacing as a NotNullViolation on
    `boards.profile_id` -- red that proves nothing about the migration. Fixed
    by creating our own profile via `profile_factory`, the same fixture the
    other three tests in this file already use.
    """
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            assert "auto_publish" in {c["name"] for c in inspect(conn).get_columns("boards")}

        # SEED OUR OWN auto row. Do not rely on `test_approved_by_accepts_null`
        # having run first: that is definition order on one xdist worker today,
        # and a silent no-op the day the file is reordered or split. A downgrade
        # that deletes nothing proves nothing about the DELETE.
        pid, uid = profile_factory(name="0078 round trip profile")
        with engine.begin() as conn:
            conn.execute(_BOARD_INSERT, {"slug": "c" * 16, "p": pid, "u": uid})
            bid = conn.execute(text("SELECT id FROM boards WHERE slug = :s"),
                               {"s": "c" * 16}).scalar()
            src = conn.execute(text("SELECT id FROM sources WHERE slug = 'vinnova'")).scalar()
            conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, url, title, deadline) VALUES "
                    "(:s, 'x-0078rt', 'https://example.org/rt', 't', now() + interval '30 days')"
                ),
                {"s": src},
            )
            cid = conn.execute(
                text("SELECT id FROM calls WHERE external_id = 'x-0078rt'")
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO board_items (board_id, call_id, position, approved_by, "
                    "qr_modules) VALUES (:b, :c, 0, NULL, 0)"
                ),
                {"b": bid, "c": cid},
            )
            before = conn.execute(
                text("SELECT count(*) FROM board_items WHERE approved_by IS NULL")
            ).scalar()
        assert before > 0, "the seed did not land; the DELETE below would prove nothing"

        command.downgrade(_cfg(pg_url), "0077")
        with engine.connect() as conn:
            cols = {c["name"] for c in inspect(conn).get_columns("boards")}
            assert "auto_publish" not in cols
            assert "board_suppressions" not in inspect(conn).get_table_names()
            # Verify the DELETE by ROW COUNT, not by the ALTER COLUMN's absence
            # of an exception -- a downgrade that deletes nothing would also
            # raise nothing right up until the NOT NULL constraint, so an
            # exception-free downgrade alone proves nothing about the DELETE.
            remaining = conn.execute(
                text("SELECT count(*) FROM board_items WHERE board_id = :b AND call_id = :c"),
                {"b": bid, "c": cid},
            ).scalar()
            assert remaining == 0, "the seeded NULL-approved row survived the downgrade's DELETE"
    finally:
        # ALWAYS restore head, even if an assertion above failed -- later tests
        # in this session reuse the shared container at head.
        command.upgrade(_cfg(pg_url), "head")
        engine.dispose()
