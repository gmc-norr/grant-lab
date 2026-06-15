from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from call_manager.db.base import Base
from call_manager.db.session import make_engine


def test_model_no_longer_defines_table():
    assert "magic_link_tokens" not in Base.metadata.tables


def test_table_dropped_at_head(migrated_engine):
    assert "magic_link_tokens" not in inspect(migrated_engine).get_table_names()


def test_0049_0050_round_trip(pg_url):
    # Serial test suite: downgrade past both, assert, then restore head.
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    eng = make_engine(pg_url)
    try:
        command.downgrade(cfg, "0048")
        cols = {c["name"] for c in inspect(eng).get_columns("users")}
        assert "password_hash" not in cols
        assert "magic_link_tokens" in inspect(eng).get_table_names()
        command.upgrade(cfg, "head")
        cols = {c["name"] for c in inspect(eng).get_columns("users")}
        assert "password_hash" in cols
        assert "magic_link_tokens" not in inspect(eng).get_table_names()
    finally:
        eng.dispose()
