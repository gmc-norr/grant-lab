import pytest
from testcontainers.postgres import PostgresContainer

from call_monitor.db.session import make_engine


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
