from sqlalchemy import text

from call_manager.db.session import make_session_factory


def test_engine_executes_select_one(db_engine):
    with db_engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
    assert result == 1


def test_session_factory_yields_working_session(db_engine):
    SessionLocal = make_session_factory(db_engine)
    with SessionLocal() as session:
        assert session.execute(text("SELECT 42")).scalar() == 42
