from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, class_=Session)


def get_db_dependency(session_factory: sessionmaker[Session]):
    """Return a FastAPI dependency that yields a session."""

    def _get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    return _get_db
