"""Per-workspace Postgres advisory locks.

Spec mandate (2026-04-18 design line 195): each drafting subprocess
holds `pg_advisory_lock(hashtext('workspace:' || workspace_id::text))`
for its lifetime. The unique partial index on `turn_jobs.workspace_id
WHERE status='running'` (Slice A Part 2 migration 0026) is the database-
level safety net, but advisory locks let the application see contention
without trying-and-failing on an INSERT.

We use the try-variant (`pg_try_advisory_lock`) so a second attempt
returns False rather than blocking. The registry (registry.py) treats
False as "another process is already handling this workspace — back
off".

This module houses TWO lock families: (a) the original session-scoped,
non-blocking, subprocess-lifetime workspace lock acquired via
``pg_try_advisory_lock`` and held until the subprocess exits or the
connection closes; and (b) the transaction-scoped, blocking commit mutex
acquired via ``pg_advisory_xact_lock`` that serializes every git mutation
of the workspace repo (user section saves, citation inserts, agent tool
calls, turn enqueue) — this lock is released automatically at COMMIT or
ROLLBACK without any explicit unlock call.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker


def _advisory_lock_key(namespace: str, workspace_id: uuid.UUID) -> int:
    digest = hashlib.sha256(f"{namespace}:{workspace_id}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 2**63:
        return unsigned - 2**64
    return unsigned


def workspace_lock_key(workspace_id: uuid.UUID) -> int:
    """Stable 64-bit signed integer derived from the workspace UUID.

    We can't use Postgres' hashtext() at advisory-lock-key time because
    we want the same key from every connection (hashtext is text-input,
    not deterministic across Postgres versions). Use a Python-side
    sha256 truncated to 64 bits, then map into the signed-int range.
    """
    return _advisory_lock_key("workspace", workspace_id)


def acquire_workspace_lock(session: Session, workspace_id: uuid.UUID) -> bool:
    """Non-blocking acquire. Returns True on success, False if held by
    another session.

    The session holds the lock until release_workspace_lock() OR until the
    session ends (Postgres releases all session-level advisory locks on
    disconnect / rollback-after-error)."""
    key = workspace_lock_key(workspace_id)
    result = session.execute(
        text("SELECT pg_try_advisory_lock(:k)"),
        {"k": key},
    ).scalar()
    return bool(result)


def release_workspace_lock(session: Session, workspace_id: uuid.UUID) -> bool:
    """Returns True if we held the lock and successfully released it;
    False if we didn't hold it (Postgres returns false in that case)."""
    key = workspace_lock_key(workspace_id)
    result = session.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": key},
    ).scalar()
    return bool(result)


class LockNotAcquired(RuntimeError):
    """Raised by workspace_lock_session when the advisory lock is already
    held by another session — typically another web instance in a
    multi-instance deployment (out of scope for v1)."""

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"workspace {workspace_id} advisory lock held by another process")
        self.workspace_id = workspace_id


@contextmanager
def workspace_lock_session(
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
) -> Iterator[Session]:
    """Open a session, acquire the workspace's advisory lock, yield the
    session. On exit (normal or exceptional), release the lock and
    close the session. Raises LockNotAcquired if the lock is held by
    another process.

    The session held by this context manager is what carries the lock —
    Postgres advisory locks are session-scoped. Most callers (the
    drafting registry) bypass this and use the low-level helpers
    directly because they need the lock to outlive any `with` block —
    the lock has to live as long as the subprocess does. This helper
    is for scoped-acquisition use cases (admin scripts, recovery).
    """
    session = session_factory()
    try:
        if not acquire_workspace_lock(session, workspace_id):
            session.close()
            raise LockNotAcquired(workspace_id)
        try:
            yield session
        finally:
            release_workspace_lock(session, workspace_id)
    finally:
        session.close()


def commit_mutex_key(workspace_id: uuid.UUID) -> int:
    """Key for the per-workspace COMMIT MUTEX — a transaction-scoped advisory
    lock serializing every git mutation of the workspace repo (user section
    saves, user citation inserts, agent mutation tools, turn enqueue).

    Distinct namespace from workspace_lock_key (the session-scoped subprocess
    lifetime lock) so holding one never implies the other.
    """
    return _advisory_lock_key("commit", workspace_id)


class CommitMutexTimeout(RuntimeError):
    """The per-workspace commit mutex could not be acquired within the
    timeout — another writer is wedged or unusually slow. Web routes map
    this to 503 'workspace is busy'."""


def acquire_commit_mutex(
    session: Session, workspace_id: uuid.UUID, *, timeout_seconds: int = 10
) -> None:
    """Blocking acquire of the commit mutex on *session*'s current
    transaction. Released automatically at COMMIT or ROLLBACK — there is no
    explicit release. Callers: acquire, do guards + git write + DB rows,
    commit. Critical sections are short (one git commit), so blocking (not
    try-) semantics are right — but bounded: raises :class:`CommitMutexTimeout`
    after *timeout_seconds* so a wedged holder can't pin FastAPI workers
    indefinitely.

    Requires a non-AUTOCOMMIT session: an AUTOCOMMIT connection would release
    the xact lock immediately and silently provide no exclusion.
    """
    # SET LOCAL cannot take a bind parameter in Postgres — validate and
    # inline the integer instead. SET LOCAL scopes to the current
    # transaction, so a rollback/commit clears it regardless.
    timeout_ms = int(timeout_seconds) * 1000
    if timeout_ms <= 0:
        raise ValueError(f"timeout_seconds must be a positive int, got {timeout_seconds!r}")
    session.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
    try:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": commit_mutex_key(workspace_id)},
        )
    except OperationalError as exc:
        # 55P03 = lock_not_available (lock_timeout fired). Anything else is a
        # real connection/server problem — re-raise unchanged. No SET LOCAL
        # restore here: the transaction is already aborted; the caller's
        # rollback discards the LOCAL setting along with everything else.
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if sqlstate == "55P03":
            raise CommitMutexTimeout(str(workspace_id)) from exc
        raise
    # Restore the default for the rest of the transaction's statements (only
    # reachable on success — after a timeout the txn is aborted and this
    # statement would itself fail).
    session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
