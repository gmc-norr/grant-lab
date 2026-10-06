"""Migration 0085: ranking_remote_attempts gains error_class, tokens_in, tokens_out."""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command

_NEW = {"error_class", "tokens_in", "tokens_out"}


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _cols(engine) -> dict:
    with engine.connect() as conn:
        return {c["name"]: c for c in inspect(conn).get_columns("ranking_remote_attempts")}


def test_0085_round_trips_three_nullable_columns(migrated_engine, pg_url):
    engine = create_engine(pg_url)
    try:
        cols = _cols(engine)
        assert cols.keys() >= _NEW
        assert all(cols[c]["nullable"] for c in _NEW)
        command.downgrade(_cfg(pg_url), "0084")
        assert not (_NEW & _cols(engine).keys())
        command.upgrade(_cfg(pg_url), "head")
        assert _cols(engine).keys() >= _NEW
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
        finally:
            engine.dispose()
