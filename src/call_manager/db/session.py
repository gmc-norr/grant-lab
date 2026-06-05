from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    # Single-instance deployment runs the web app + ~9 APScheduler job types
    # against one engine. The SQLAlchemy defaults (pool_size=5, max_overflow=10)
    # can be tight under concurrent request + scheduler load; pool_recycle
    # guards against a proxy/firewall silently dropping long-idle connections.
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, class_=Session)


def get_db_dependency(session_factory: sessionmaker[Session]):
    """Return a FastAPI dependency that yields a session."""

    def _get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    return _get_db
