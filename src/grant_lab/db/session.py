from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    # Single-instance deployment runs the web app + ~9 APScheduler job types
    # against one engine. The SQLAlchemy defaults (pool_size=5, max_overflow=10)
    # can be tight under concurrent request + scheduler load; pool_recycle
    # guards against a proxy/firewall silently dropping long-idle connections.
    #
    # pool_timeout=5 (review 2026-10-26 A4), down from SQLAlchemy's default 30.
    # The 30 connections here are smaller than the anyio threadpool (40) plus
    # SCHEDULER_EXECUTOR_MAX_WORKERS (30), so exhaustion is reachable, and the
    # default turned it into half a minute of silence per waiter. Five seconds
    # fails the one request that could not get a connection fast enough to be
    # visible in the logs and to a user, instead of parking it. It does not make
    # exhaustion less likely — it makes it diagnosable. Pinned by
    # tests/test_pool_exhaustion_event_loop.py.
    #
    # Two things the arithmetic above does NOT cover. (1) The turns stream's
    # DB work now runs via `asyncio.to_thread`, which uses the asyncio DEFAULT
    # executor — `min(32, cpu+4)`, a different pool from the anyio 40 named
    # here — so under exhaustion ~32 concurrently blocked polls can queue other
    # `to_thread` callers. The event loop still stays free, which is the point;
    # it is a bounded degradation replacing a total freeze. (2) This factory
    # also backs the ingestion/embeddings/backfill CLIs and the test
    # `db_engine` fixture, so a starved checkout under `uv run pytest -n 16`
    # now fails in 5 s rather than 30. Neither has been observed.
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        pool_timeout=5,
    )


def make_drafting_engine(database_url: str, *, pool_size: int) -> Engine:
    """Dedicated engine for drafting advisory-lock sessions (Defect 1).

    Each live drafting subprocess holds a Postgres SESSION-level advisory lock
    on a checked-out connection for its whole life (up to
    WORKSPACE_IDLE_TIMEOUT_SECONDS). On the shared request engine that pins one
    of its 30 connections per live workspace, so ~30 active workspaces would
    starve every web request and scheduler tick. Routing those long-held lock
    sessions through a SEPARATE small pool isolates them: even at the
    DRAFTING_MAX_LIVE_SUBPROCESSES ceiling no drafting connection is drawn from
    the request pool.

    pool_size is sized to the ceiling; max_overflow=1 gives a single connection
    of head room for the brief launch-vs-eviction overlap (the new subprocess
    connects before the evicted one's connection is fully released) while still
    capping the pool just above the ceiling — it can't balloon. pool_pre_ping +
    pool_recycle mirror the request engine's long-lived-connection hygiene.
    """
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=1,
        pool_recycle=1800,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False keeps ORM attributes usable after commit (the
    # request-scoped dependency reads response data post-commit). This factory
    # ALSO backs the APScheduler workers, where stale reads would be a hazard
    # IF a worker held one session across multiple commits and reused
    # previously-loaded objects. INVARIANT: every worker opens a FRESH session
    # per unit of work and re-`select()`s the rows it acts on each tick — the
    # ranking worker re-opens a session per job, the digest loop per user, the
    # reaper/drain per sweep. Preserve that pattern; do not thread a single
    # long-lived session through a polling loop under this factory.
    return sessionmaker(engine, expire_on_commit=False, class_=Session)


def get_db_dependency(session_factory: sessionmaker[Session]):
    """Return a FastAPI dependency that yields a session."""

    def _get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    return _get_db
