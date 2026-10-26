import asyncio
import os
from contextlib import ExitStack

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import close_all_sessions
from testcontainers.postgres import PostgresContainer

from alembic import command
from grant_lab.config import Settings
from grant_lab.db.base import Base
from grant_lab.db.session import make_engine, make_session_factory


class _ExplicitEventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    def get_event_loop(self):
        loop = getattr(self._local, "_loop", None)
        if loop is None:
            raise RuntimeError("There is no current event loop")
        return loop


@pytest.fixture(scope="session", autouse=True)
def _require_explicit_event_loops():
    previous_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(_ExplicitEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous_policy)


# Hermetic git for the whole suite. Deliberately carries NO identity: see
# _hermetic_git_env below.
_HERMETIC_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    # One extra config pair, injected at the highest precedence level, turning
    # off git's ident auto-detection. Nothing else in src/ or tests/ uses the
    # GIT_CONFIG_COUNT mechanism, so index 0 is free.
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "user.useConfigOnly",
    "GIT_CONFIG_VALUE_0": "true",
}


@pytest.fixture(scope="session", autouse=True)
def _hermetic_git_env():
    """Make git in tests see what CI sees: no ambient identity at all.

    CI runs as a user with an empty GECOS field and no global or system git
    config, so every `git commit` that does not pass an identity dies with
    `fatal: empty ident name`. The dev box has both, so six collab tests passed
    here and failed there (review 2026-10-26, A1 finding 3) — and, worse, the
    production identity in `citations/gitio.py:_git_env` and
    `phases/setup.py:_git_env` was not load-bearing locally: deleting all eight
    `setdefault("GIT_AUTHOR_*"/"GIT_COMMITTER_*")` lines left test_editing_save,
    test_phase0_setup, test_accepting and test_gitio_timeouts 52/52 green on the
    dev box, against 15 red in a clean container. With this fixture those same
    52 are 15 red / 37 passed HERE — the clean container, reproduced exactly
    (measured 2026-10-26).

    Hiding the global config is NOT enough on this box (measured 2026-10-26):
    git then guesses the name from GECOS and the six tests still pass. Hence
    `user.useConfigOnly=true`, which turns auto-detection off — a bare
    `git commit` then exits 128 with "no email was given and auto-detection is
    disabled", while one carrying `GIT_AUTHOR_*`/`GIT_COMMITTER_*` succeeds.

    Repo-local `git config user.email` still works, which is why the ~50 other
    test helpers that write identity into the repo config are unaffected.

    This sets `os.environ` directly rather than via `monkeypatch`, which is
    function-scoped and cannot hold a session-scoped value. Under
    `pytest -n 16 --dist loadfile` every xdist worker is its own process with
    its own `os.environ`, so this runs once per worker and there is nothing to
    race: no worker can observe another's environment.

    Do NOT add an identity here. That would restore exactly the blindness this
    fixture exists to remove.
    """
    previous = {key: os.environ.get(key) for key in _HERMETIC_GIT_ENV}
    os.environ.update(_HERMETIC_GIT_ENV)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _isolate_golden_set_files(tmp_path, monkeypatch):
    """Keep the suite out of the human's live `data/` directory.

    `ranking/eval.py` computes `_REPORT_PATH` and `_HISTORY_PATH` at import,
    both under `CALL_MANAGER_DATA_DIR` (default `data/`), so they are absolute
    paths into the working tree by the time any test runs. Redirecting them per
    module did not hold: `test_ranking_eval.py` patched `_REPORT_PATH` and not
    `_HISTORY_PATH`, and `test_golden_set_eval_db_mode.py` did the same, so a
    run appended **six** rows to the real `data/golden_set_history.jsonl`
    (measured 2026-09-05, 1120 -> 1126 lines). Doing it here means a new test
    cannot forget. `tests/test_golden_set_isolation.py` fails if either
    constant ever resolves back inside the repo's data dir.
    """
    from grant_lab.ranking import eval as eval_mod

    monkeypatch.setattr(eval_mod, "_REPORT_PATH", tmp_path / "golden_set_last.json")
    monkeypatch.setattr(eval_mod, "_HISTORY_PATH", tmp_path / "golden_set_history.jsonl")


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch):
    """Disable .env loading for every test.

    A developer's local .env can otherwise pre-fill SESSION_SECRET,
    INITIAL_ADMIN_EMAIL, etc., turning hermetic env-driven tests into
    flaky pass-on-my-machine traps. Tests that exercise Settings() must
    rely solely on monkeypatch.setenv()."""
    # SettingsConfigDict is a TypedDict; mutate via dict-item assignment.
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def test_client_factory():
    """Create entered TestClients and close their app lifespans after each test."""
    with ExitStack() as stack:

        def create(app, **kwargs):
            return stack.enter_context(TestClient(app, **kwargs))

        yield create


@pytest.fixture(scope="session")
def _test_workspaces_root(tmp_path_factory):
    """One throwaway root for every workspace repo the suite creates."""
    return tmp_path_factory.mktemp("workspaces")


@pytest.fixture(autouse=True)
def _workspaces_dir_is_never_the_real_one(monkeypatch, _test_workspaces_root):
    """Point WORKSPACES_DIR at a temp root for every test.

    Settings.workspaces_dir defaults to "var/workspaces" — the SAME directory a
    real deployment uses, and the one this repo's own .env names. Tests that build
    a Settings() (or an app) without overriding it therefore created real workspace
    repos in the operator's live data directory: a measured 16 per full-suite run,
    which had accumulated to 488 of the 497 directories there. Nothing was
    corrupted (tests use their own database and fresh UUIDs), but it made the
    directory useless as evidence — a survey of "how many workspaces have branches"
    was really counting test fixtures.

    `_isolate_settings_from_dotenv` above does not cover this: the value came from
    the field DEFAULT, not from .env. An env var beats both.

    Pinned by tests/architecture/test_workspaces_dir_isolation.py.
    """
    monkeypatch.setenv("WORKSPACES_DIR", str(_test_workspaces_root))


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
    # Controllers keep one session checked out for each live subprocess advisory
    # lock. Engine.dispose() cannot close checked-out sessions, so close every
    # session before resetting the database and pool at the test boundary.
    close_all_sessions()
    # Truncate every table Base knows about, sorted for determinism. Future
    # plans that add tables get isolation for free without having to remember
    # to update this fixture. CASCADE handles FK ordering; RESTART IDENTITY
    # resets sequences so id counters are predictable across tests.
    table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
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
    # Advisory-lock isolation: pg_advisory_lock is session(connection)-scoped and
    # the engine pools connections. A test that strands a lock (e.g. launches a
    # workspace handle without terminating it) leaves it on a pooled connection,
    # which would later surface as a spurious "workspace is busy" in an unrelated
    # test that contends for that connection. Disposing the pool at each test
    # boundary closes those connections so their advisory locks are released.
    migrated_engine.dispose()


@pytest.fixture()
def profile_factory(pg_session_factory):
    """Yield a factory that creates a Profile owned by a fresh User.
    Returns the (profile_id, user_id) tuple so tests can use the IDs
    after the session closes."""
    from grant_lab.db.models import Profile, ProfileKind, User

    created: list[int] = []

    def _make(
        *,
        name: str = "Group A",
        kind: ProfileKind = ProfileKind.ORGANIZATIONAL_UNIT,
        level: str | None = "group",
        description: str = "Cancer immunotherapy and digital pathology research.",
        structured: dict | None = None,
    ) -> tuple[int, int]:
        with pg_session_factory() as s:
            u = User(email=f"owner-{len(created)}@umu.se", status="active")
            s.add(u)
            s.flush()
            p = Profile(
                name=name,
                kind=kind,
                level=level if kind == ProfileKind.ORGANIZATIONAL_UNIT else None,
                description=description,
                structured=structured or {},
                created_by=u.id,
            )
            s.add(p)
            s.commit()
            created.append(p.id)
            return p.id, u.id

    yield _make


@pytest.fixture(autouse=True)
def _close_helper_entered_lifespans():
    """Close every app lifespan opened by _refs_helpers.authed_client.

    That helper enters a TestClient lifespan and returns the client; its ~60 call
    sites use it inline, so nothing ever exits it. Each unclosed lifespan leaves a
    started APScheduler alive for the rest of the session — one whose
    _analysis_drain_tick needs no app_loop and so keeps draining the shared
    analysis queue under other tests, while holding the single-instance advisory
    lock that test_scheduler_single_instance asserts is free.

    Imported lazily so this root conftest does not pull in grant_lab.main at
    collection time. A no-op for the vast majority of tests, which never call it.
    """
    yield
    from tests.proposal_assist._refs_helpers import close_entered_clients

    close_entered_clients()
