import pytest
from alembic.config import Config
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from alembic import command
from call_monitor.db.session import make_engine, make_session_factory


@pytest.fixture(scope="session")
def pg_container():
    # driver="psycopg" makes get_connection_url() emit `postgresql+psycopg://...`
    # directly. No post-hoc URL rewrite, so the fixture is robust if testcontainers
    # ever changes its default driver.
    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_url(pg_container):
    return pg_container.get_connection_url()


@pytest.fixture(scope="session")
def db_engine(pg_url):
    """Session-scoped Engine: shared across tests, disposed at session teardown.

    Without this, tests that build their own Engine via `make_engine(pg_url)`
    leak DBAPI connections to the GC and emit `ResourceWarning` (which become
    fatal under `filterwarnings = ["error"]`). Centralizing here also avoids
    spinning up a fresh engine per test.
    """
    engine = make_engine(pg_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def migrated_engine(db_engine, pg_url):
    """Same engine as `db_engine`, but with Alembic migrations applied first.

    Reuses `db_engine` (single shared, disposed at session teardown) to keep
    connection accounting clean.
    """
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(cfg, "head")
    return db_engine


@pytest.fixture()
def pg_session_factory(migrated_engine):
    SessionLocal = make_session_factory(migrated_engine)
    yield SessionLocal
    # truncate all tables between tests for isolation
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE users, magic_link_tokens RESTART IDENTITY CASCADE"
            )
        )
