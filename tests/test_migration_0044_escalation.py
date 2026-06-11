from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_0044_adds_columns_ledger_and_backfills_analysis_model(pg_url):
    from call_manager.db.session import make_engine

    cfg = _cfg(pg_url)
    # Arrange: at 0043, insert a call_rankings row WITH a summary so the
    # backfill has something to copy model→analysis_model.
    # Upgrade to head first so subsequent downgrade to 0043 is always valid
    # regardless of where the shared pg_container left off.
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0043")
    engine = make_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S','s44') ON CONFLICT DO NOTHING")
            )
            sid = conn.execute(text("SELECT id FROM sources WHERE slug='s44'")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, url, title, keywords, raw) "
                    "VALUES (:s,'e','u','t','{}','{}')"
                ),
                {"s": sid},
            )
            cid = conn.execute(text("SELECT id FROM calls WHERE external_id='e'")).scalar_one()
            conn.execute(text("INSERT INTO users (email) VALUES ('m44@ki.se')"))
            uid = conn.execute(text("SELECT id FROM users WHERE email='m44@ki.se'")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO profiles (name, kind, description, structured, created_by) "
                    "VALUES ('P44','organizational_unit','d','{}',:u)"
                ),
                {"u": uid},
            )
            pid = conn.execute(text("SELECT id FROM profiles WHERE name='P44'")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO call_rankings (call_id, profile_id, aggregate_score, "
                    "rationale, model, prompt_version, raw_response, summary) "
                    "VALUES (:c,:p,80,'r','claude-sonnet-4-6','v5.0','{}','an analysis')"
                ),
                {"c": cid, "p": pid},
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0044")
    engine = make_engine(pg_url)
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("call_rankings")}
        assert {"escalated", "escalated_from_score", "analysis_model"} <= cols
        assert "ranking_remote_attempts" in inspect(engine).get_table_names()
        with engine.connect() as conn:
            am = conn.execute(
                text("SELECT analysis_model FROM call_rankings WHERE summary IS NOT NULL")
            ).scalar_one()
        assert am == "claude-sonnet-4-6"  # backfilled from model
    finally:
        engine.dispose()

    command.downgrade(cfg, "0043")
    engine = make_engine(pg_url)
    try:
        assert "ranking_remote_attempts" not in inspect(engine).get_table_names()
        assert "escalated" not in {c["name"] for c in inspect(engine).get_columns("call_rankings")}
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")

    # Migration tests bypass the pg_session_factory teardown, so they must
    # clean their own residue (order-independence of the suite).
    engine = make_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE TABLE ranking_remote_attempts, call_rankings, calls, "
                    "profiles, users, sources RESTART IDENTITY CASCADE"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO sources (name, slug) VALUES "
                    "('EU Funding & Tenders Portal', 'eu_portal'), "
                    "('Vinnova', 'vinnova')"
                )
            )
    finally:
        engine.dispose()
