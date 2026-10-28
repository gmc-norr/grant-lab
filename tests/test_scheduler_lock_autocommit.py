"""Review 2026-10-26 (A4 and A2): the scheduler's advisory-lock connection sat
`idle in transaction` for 20 days.

`probe_scheduler_lock` borrowed a connection with `engine.connect()` and ran
`pg_try_advisory_lock` on it. SQLAlchemy autobegins a transaction on the first
statement, and nothing ever commits or rolls it back — so the backend holding
the single-instance lock stays in an open transaction for the process lifetime.
Live `pg_stat_activity` showed pid 174416 with `xact_start 2026-10-06 14:55`,
state `idle in transaction`.

Two concrete consequences, both of which these tests pin:

* `idle_in_transaction_session_timeout` is standard Postgres hardening. Setting
  it kills exactly this session, which drops the lock, which stops the
  scheduler and turns `/healthz` 503 (A4; A2 "Risk 1").
* Any statement error on that connection leaves the transaction ABORTED, after
  which `is_held()`'s `SELECT 1` can only raise `InFailedSqlTransaction` — so
  the watchdog reports the lock lost forever while Postgres still holds it, and
  stops the scheduler on a lie (A2 "Risk 2").

The fix is `execution_options(isolation_level="AUTOCOMMIT")` on the lock
connection. A session-level `pg_advisory_lock` is NOT released at commit (only
`pg_advisory_xact_lock` is), so autocommit keeps the lock for the connection's
life — which is what `test_an_autocommit_lock_is_still_held_against_a_second_instance`
verifies directly rather than taking on trust.
"""

from __future__ import annotations

import time

from sqlalchemy import create_engine, text

from grant_lab.scheduler import SCHEDULER_LOCK_NAME, SchedulerLock, probe_scheduler_lock


def _backend_row(engine, pid: int) -> tuple[str | None, bool | None]:
    """`(state, xact_start is null)` for one backend, read from a SECOND
    connection — exactly the observable the reviewer used on the live DB."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT state, xact_start IS NULL FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": pid},
        ).one_or_none()
    return (None, None) if row is None else (row[0], row[1])


def _wait_for_state(engine, pid: int, wanted: str, *, timeout: float = 5.0):
    """`pg_stat_activity.state` is maintained by the backend itself, so give it a
    moment to settle. Returns the LAST row seen, so a failing assertion can name
    what it actually found."""
    deadline = time.monotonic() + timeout
    row = _backend_row(engine, pid)
    while row[0] != wanted and time.monotonic() < deadline:
        time.sleep(0.05)
        row = _backend_row(engine, pid)
    return row


def test_the_lock_connection_is_not_left_idle_in_transaction(pg_url):
    """The live symptom, measured the way the reviewer measured it: a second
    connection reads `pg_stat_activity` for the lock's own backend.

    NOTE on what is NOT asserted here. `Connection.in_transaction()` stays True
    under AUTOCOMMIT — SQLAlchemy still autobegins its own logical
    RootTransaction, it simply emits no BEGIN. Measured on this fix:
    `in_transaction(): True`, `get_execution_options(): {'isolation_level':
    'AUTOCOMMIT'}`, `pg state: idle`, `xact_start IS NULL: True`. The
    SQLAlchemy-side flag is therefore not the fact that matters; Postgres's own
    view is, and `xact_start` is precisely what A4 reported as stuck at
    2026-10-06 14:55.
    """
    engine_a = create_engine(pg_url)
    engine_b = create_engine(pg_url)
    try:
        lock, state = probe_scheduler_lock(engine_a)
        assert isinstance(lock, SchedulerLock) and state == "acquired"
        try:
            pid = lock._connection.execute(text("SELECT pg_backend_pid()")).scalar()
            observed, no_xact = _wait_for_state(engine_b, pid, "idle")
            assert observed == "idle", (
                f"pg_stat_activity.state for the lock backend is {observed!r}, "
                "not 'idle' (A4 found 'idle in transaction' for 20 days, which "
                "idle_in_transaction_session_timeout would kill)"
            )
            assert no_xact is True, (
                "the lock backend has an open transaction (xact_start is set); "
                "A4 found xact_start 20 days old on the live lock connection"
            )
        finally:
            lock.release()
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_an_autocommit_lock_is_still_held_against_a_second_instance(pg_url):
    """The guard that keeps the fix from weakening the lock.

    A fix that moved the lock to a transaction-scoped lock, or that released it
    at commit, would make the first test pass and silently destroy the
    single-instance constraint. So: while this process holds it, a second
    connection must FAIL to take it, `pg_locks` must show it, and after
    `release()` the next instance must be able to take it.
    """
    engine_a = create_engine(pg_url)
    engine_b = create_engine(pg_url)
    try:
        lock, state = probe_scheduler_lock(engine_a)
        assert isinstance(lock, SchedulerLock) and state == "acquired"
        released = False
        try:
            # Several statements AFTER the lock, because under AUTOCOMMIT each
            # one commits — the point being that none of them drops the lock.
            for _ in range(3):
                assert lock._connection.execute(text("SELECT 1")).scalar() == 1
            pid = lock._connection.execute(text("SELECT pg_backend_pid()")).scalar()

            with engine_b.connect() as conn_b:
                assert (
                    conn_b.execute(
                        text("SELECT pg_try_advisory_lock(hashtext(:n))"),
                        {"n": SCHEDULER_LOCK_NAME},
                    ).scalar()
                    is False
                ), "a second instance took the lock — autocommit released it"
                held_by = conn_b.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND pid = :pid AND granted"
                    ),
                    {"pid": pid},
                ).scalar()
                assert held_by == 1, f"pg_locks shows {held_by} advisory locks on the lock backend"

            assert lock.is_held() is True
            lock.release()
            released = True
            with engine_b.connect() as conn_b:
                assert (
                    conn_b.execute(
                        text("SELECT pg_try_advisory_lock(hashtext(:n))"),
                        {"n": SCHEDULER_LOCK_NAME},
                    ).scalar()
                    is True
                ), "release() did not free the lock"
                conn_b.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:n))"), {"n": SCHEDULER_LOCK_NAME}
                )
        finally:
            if not released:
                lock.release()
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_a_failed_statement_does_not_make_the_lock_look_lost_forever(pg_url):
    """A2 "Risk 2": with an open transaction, one bad statement poisons it, and
    `is_held()` then answers False for the rest of the process — so the
    watchdog stops the scheduler while Postgres still holds the lock."""
    from sqlalchemy.exc import DatabaseError

    engine_a = create_engine(pg_url)
    engine_b = create_engine(pg_url)
    try:
        lock, state = probe_scheduler_lock(engine_a)
        assert isinstance(lock, SchedulerLock) and state == "acquired"
        try:
            try:
                lock._connection.execute(text("SELECT this_function_does_not_exist()"))
            except DatabaseError:
                pass
            else:  # pragma: no cover - the statement must fail
                raise AssertionError("the deliberately broken statement did not fail")

            assert lock.is_held() is True, (
                "one failed statement left the lock connection in an aborted "
                "transaction, so is_held() reports the lock lost while Postgres "
                "still holds it"
            )
            # And it really is still held, not merely reported as held.
            with engine_b.connect() as conn_b:
                assert (
                    conn_b.execute(
                        text("SELECT pg_try_advisory_lock(hashtext(:n))"),
                        {"n": SCHEDULER_LOCK_NAME},
                    ).scalar()
                    is False
                )
        finally:
            lock.release()
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_a_failure_setting_autocommit_closes_the_connection_it_opened(pg_url, monkeypatch):
    """`probe_scheduler_lock` opens the connection and sets AUTOCOMMIT in TWO
    steps, not one chained expression.

    Chained, a raise from `execution_options` would leave the `Connection` that
    `connect()` returned unreferenced, so the broad handler would return
    `"unavailable"` without closing it — leaking one of the pool's connections
    AND starting the app with single-instance enforcement silently off. That
    second half is why this one code path earns a test: "silently off" is the
    expensive outcome here, and a leak that only GC cleans up is invisible.

    `execution_options` cannot raise today. This pins the SHAPE, so the two-step
    form is not collapsed back into one by a later tidy-up (review of Task 22,
    2026-10-28)."""
    import sqlalchemy

    engine = create_engine(pg_url, pool_size=1, max_overflow=0, pool_timeout=2)
    try:
        def _boom(self, *a, **kw):
            raise RuntimeError("dialect refused the isolation level")

        monkeypatch.setattr(sqlalchemy.engine.Connection, "execution_options", _boom)

        lock, reason = probe_scheduler_lock(engine)

        assert lock is None
        assert reason == "unavailable", reason
        # The real observable: the one connection the pool has is back. On the
        # chained form this is 1, and with pool_size=1 the next probe would
        # block for pool_timeout and then fail too.
        assert engine.pool.checkedout() == 0, (
            f"the connection opened before the failure was leaked; "
            f"checkedout={engine.pool.checkedout()}"
        )

        # And the pool is genuinely reusable afterwards.
        monkeypatch.undo()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()
