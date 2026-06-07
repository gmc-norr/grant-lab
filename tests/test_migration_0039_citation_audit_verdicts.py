"""Round-trip checks for migration 0039 (citation_audit_verdicts).

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


def _tables(conn):
    return set(inspect(conn).get_table_names())


def test_0039_round_trip(migrated_engine, pg_url):
    engine = create_engine(pg_url)
    try:
        # At head: the table + its columns exist.
        with engine.connect() as conn:
            assert "citation_audit_verdicts" in _tables(conn)
            assert _columns(conn, "citation_audit_verdicts") == {
                "id",
                "workspace_id",
                "cache_key",
                "verdict",
                "rationale",
                "validator_version",
                "created_at",
            }

        # Downgrade to 0038: the table is gone.
        command.downgrade(_cfg(pg_url), "0038")
        with engine.connect() as conn:
            assert "citation_audit_verdicts" not in _tables(conn)
    finally:
        # ALWAYS restore head, even if an assertion above failed — later tests
        # in this session reuse the shared container at head.
        command.upgrade(_cfg(pg_url), "head")
        engine.dispose()
