from alembic.config import Config
from sqlalchemy import text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_0045_nulls_profile_vectors_only(pg_url):
    from call_manager.db.session import make_engine

    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0044")
    engine = make_engine(pg_url)
    vec = "[" + ",".join("0.1" for _ in range(1024)) + "]"
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S','s45') ON CONFLICT DO NOTHING")
            )
            sid = conn.execute(text("SELECT id FROM sources WHERE slug='s45'")).scalar_one()
            conn.execute(text("INSERT INTO users (email) VALUES ('m45@umu.se')"))
            uid = conn.execute(text("SELECT id FROM users WHERE email='m45@umu.se'")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO profiles (name, kind, description, structured, created_by, "
                    "embedding, embedding_model) VALUES "
                    "('P45','organizational_unit','d','{}',:u, :v, 'bge-m3')"
                ),
                {"u": uid, "v": vec},
            )
            conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, url, title, keywords, raw, "
                    "embedding, embedding_model) VALUES "
                    "(:s,'e45','u','t','{}','{}', :v, 'bge-m3')"
                ),
                {"s": sid, "v": vec},
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "0045")
    engine = make_engine(pg_url)
    try:
        with engine.connect() as conn:
            pe = conn.execute(
                text("SELECT embedding, embedding_model FROM profiles WHERE name='P45'")
            ).one()
            ce = conn.execute(
                text("SELECT embedding_model FROM calls WHERE external_id='e45'")
            ).scalar_one()
        assert pe[0] is None and pe[1] is None  # profile vector NULLed
        assert ce == "bge-m3"  # call vector untouched
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
