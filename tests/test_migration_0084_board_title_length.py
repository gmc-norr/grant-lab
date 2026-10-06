"""Migration 0084: ck_boards_title_length (review 2026-10-06 M10)."""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _checks(conn) -> set[str]:
    return {c["name"] for c in inspect(conn).get_check_constraints("boards")}


def test_0084_trims_an_overlong_title_then_adds_the_check(migrated_engine, pg_url, profile_factory):
    pid, owner_id = profile_factory(name="M0084 profile")
    engine = create_engine(pg_url)
    try:
        command.downgrade(_cfg(pg_url), "0083")
        with engine.begin() as conn:
            assert "ck_boards_title_length" not in _checks(conn)
            bid = conn.execute(
                text(
                    "INSERT INTO boards (slug, title, profile_id, created_by, orientation, "
                    "items_per_page) VALUES ('m0084-slug-sixteen', repeat('x', 900), :p, :u, "
                    "'landscape', 8) RETURNING id"
                ),
                {"p": pid, "u": owner_id},
            ).scalar_one()

        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            assert "ck_boards_title_length" in _checks(conn)
            length = conn.execute(
                text("SELECT char_length(title) FROM boards WHERE id=:b"), {"b": bid}
            ).scalar_one()
        assert length == 200

        command.downgrade(_cfg(pg_url), "0083")
        with engine.connect() as conn:
            assert "ck_boards_title_length" not in _checks(conn)
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM boards WHERE slug='m0084-slug-sixteen'"))
        finally:
            engine.dispose()
