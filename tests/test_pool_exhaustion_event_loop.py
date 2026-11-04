"""Review 2026-10-26 A4: make_engine sets pool_timeout=5, so a request that
cannot get a pooled connection fails in seconds instead of parking for
SQLAlchemy's 30 s default."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SAPoolTimeout

from grant_lab.db.session import make_engine


def test_make_engine_fails_the_request_after_five_seconds_not_thirty(pg_url):
    """`pool_timeout=5` (A4 fix): exhaustion must fail the one request that
    wanted a connection, not park it for SQLAlchemy's 30 s default."""
    engine = make_engine(pg_url)
    held: list = []
    try:
        # Positive control: a normal request gets a connection and works.
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1

        assert engine.pool.size() == 10, engine.pool.status()
        for _ in range(30):  # pool_size 10 + max_overflow 20
            held.append(engine.connect())
        assert engine.pool.checkedout() == 30, engine.pool.status()

        started = time.monotonic()
        with pytest.raises(SAPoolTimeout) as excinfo:
            engine.connect()
        elapsed = time.monotonic() - started
        assert "QueuePool limit" in str(excinfo.value), str(excinfo.value)
        assert 4.0 <= elapsed < 15.0, (
            f"a request waited {elapsed:.1f}s for a connection; pool_timeout is "
            "not the configured 5s (SQLAlchemy's default is 30s)"
        )

        # Positive control the other way: once a connection is returned, the
        # next request gets one immediately.
        held.pop().close()
        started = time.monotonic()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
        assert time.monotonic() - started < 2.0
    finally:
        for conn in held:
            conn.close()
        engine.dispose()
