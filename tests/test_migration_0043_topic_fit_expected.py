from alembic.config import Config
from sqlalchemy import inspect

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_0043_adds_nullable_topic_fit_expected(pg_url):
    from call_manager.db.session import make_engine

    cfg = _cfg(pg_url)
    command.upgrade(cfg, "0043")
    engine = make_engine(pg_url)
    try:
        cols = {c["name"]: c for c in inspect(engine).get_columns("call_rankings")}
        assert "topic_fit_expected" in cols
        assert cols["topic_fit_expected"]["nullable"] is True
    finally:
        engine.dispose()
    command.downgrade(cfg, "0042")
    engine = make_engine(pg_url)
    try:
        assert "topic_fit_expected" not in {
            c["name"] for c in inspect(engine).get_columns("call_rankings")
        }
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
