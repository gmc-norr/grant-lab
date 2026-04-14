import pytest
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_container():
    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_url(pg_container):
    # testcontainers returns a psycopg2-style URL; swap driver to psycopg v3
    url = pg_container.get_connection_url()
    return url.replace("postgresql+psycopg2://", "postgresql+psycopg://")
