import pytest
from alembic.config import Config
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from alembic import command
from call_manager.config import Settings
from call_manager.db.base import Base
from call_manager.db.session import make_engine, make_session_factory


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch):
    """Disable .env loading for every test.

    A developer's local .env can otherwise pre-fill SESSION_SECRET,
    INITIAL_ADMIN_EMAIL, etc., turning hermetic env-driven tests into
    flaky pass-on-my-machine traps. Tests that exercise Settings() must
    rely solely on monkeypatch.setenv()."""
    # SettingsConfigDict is a TypedDict; mutate via dict-item assignment.
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(scope="session")
def pg_container():
    # driver="psycopg" makes get_connection_url() emit `postgresql+psycopg://...`
    # directly. No post-hoc URL rewrite, so the fixture is robust if testcontainers
    # ever changes its default driver.
    # pgvector/pgvector:pg16 is Postgres 16 with the vector extension preinstalled.
    # Required for the embedding columns added in migration 0007.
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
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
    # Truncate every table Base knows about, sorted for determinism. Future
    # plans that add tables get isolation for free without having to remember
    # to update this fixture. CASCADE handles FK ordering; RESTART IDENTITY
    # resets sequences so id counters are predictable across tests.
    table_names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    if table_names:
        with migrated_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
            # Re-seed sources so model/orchestrator tests find them after teardown.
            conn.execute(
                text(
                    "INSERT INTO sources (name, slug) VALUES "
                    "('EU Funding & Tenders Portal', 'eu_portal'), "
                    "('Vinnova', 'vinnova')"
                )
            )


@pytest.fixture()
def profile_factory(pg_session_factory):
    """Yield a factory that creates a Profile owned by a fresh User.
    Returns the (profile_id, user_id) tuple so tests can use the IDs
    after the session closes."""
    from call_manager.db.models import Profile, ProfileKind, User

    created: list[int] = []

    def _make(
        *,
        name: str = "Group A",
        kind: ProfileKind = ProfileKind.GROUP,
        description: str = "Cancer immunotherapy and digital pathology research.",
        structured: dict | None = None,
    ) -> tuple[int, int]:
        with pg_session_factory() as s:
            u = User(email=f"owner-{len(created)}@ki.se")
            s.add(u)
            s.flush()
            p = Profile(
                name=name,
                kind=kind,
                description=description,
                structured=structured or {},
                created_by=u.id,
            )
            s.add(p)
            s.commit()
            created.append(p.id)
            return p.id, u.id

    yield _make
