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
    # This factory also backs the ingestion/embeddings CLIs and the test
    # `db_engine` fixture, so a starved checkout under `uv run pytest -n 16`
    # fails in 5 s rather than 30.
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        pool_timeout=5,
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
