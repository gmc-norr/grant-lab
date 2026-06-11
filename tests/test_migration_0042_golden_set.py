from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_0042_upgrade_creates_table_then_downgrade_drops(pg_url):
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "0042")
    from call_manager.db.session import make_engine

    engine = make_engine(pg_url)
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("golden_set_triples")}
        assert {
            "id",
            "call_id",
            "profile_id",
            "expected_score",
            "note",
            "created_by",
            "created_at",
            "updated_at",
        } <= cols
        uniques = insp.get_unique_constraints("golden_set_triples")
        assert any(set(u["column_names"]) == {"call_id", "profile_id"} for u in uniques)
        with engine.connect() as conn:
            ck = (
                conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'golden_set_triples'::regclass AND contype='c'"
                    )
                )
                .scalars()
                .all()
            )
        assert any("expected_score" in name or "score" in name for name in ck)
    finally:
        engine.dispose()
    command.downgrade(cfg, "0041")
    engine = make_engine(pg_url)
    try:
        assert "golden_set_triples" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
