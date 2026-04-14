from sqlalchemy import text

from call_monitor.db.session import make_engine, make_session_factory


def test_engine_executes_select_one(pg_url):
    engine = make_engine(pg_url)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
    assert result == 1


def test_session_factory_yields_working_session(pg_url):
    SessionLocal = make_session_factory(make_engine(pg_url))
    with SessionLocal() as session:
        assert session.execute(text("SELECT 42")).scalar() == 42
