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
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


def workspace_lock_key(workspace_id: uuid.UUID) -> int:
    """Stable 64-bit signed integer derived from the workspace UUID.

    We can't use Postgres' hashtext() at advisory-lock-key time because
    we want the same key from every connection (hashtext is text-input,
    not deterministic across Postgres versions). Use a Python-side
    sha256 truncated to 64 bits, then map into the signed-int range.
    """
    digest = hashlib.sha256(
        f"workspace:{workspace_id}".encode(),
    ).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 2**63:
        return unsigned - 2**64
    return unsigned


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
    digest = hashlib.sha256(f"commit:{workspace_id}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 2**63:
        return unsigned - 2**64
    return unsigned


def acquire_commit_mutex(session: Session, workspace_id: uuid.UUID) -> None:
    """Blocking acquire of the commit mutex on *session*'s current
    transaction. Released automatically at COMMIT or ROLLBACK — there is no
    explicit release. Callers: acquire, do guards + git write + DB rows,
    commit. Critical sections are short (one git commit), so blocking (not
    try-) semantics are right."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": commit_mutex_key(workspace_id)},
    )
