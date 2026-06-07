"""Round-trip checks for migration 0038 (reference tombstoning).

pg_url is the SHARED session container; this test downgrades then upgrades,
so it MUST leave the schema at head for later tests — done in finally.
"""

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _columns(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


def test_0038_round_trip(migrated_engine, pg_url):
    engine = create_engine(pg_url)
    try:
        # At head: the column exists.
        with engine.connect() as conn:
            assert "tombstoned_at" in _columns(conn, "references")

        # Downgrade to 0037: the column is gone.
        command.downgrade(_cfg(pg_url), "0037")
        with engine.connect() as conn:
            assert "tombstoned_at" not in _columns(conn, "references")
    finally:
        # ALWAYS restore head, even if an assertion above failed — later tests
        # in this session reuse the shared container at head.
        command.upgrade(_cfg(pg_url), "head")
        engine.dispose()
