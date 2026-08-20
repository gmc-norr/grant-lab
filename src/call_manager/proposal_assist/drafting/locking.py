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

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import Workspace
from call_manager.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE as BUSY_ERROR_MESSAGE,
)
from call_manager.proposal_assist.ports.errors import (
    DELETED_ERROR_MESSAGE as DELETED_ERROR_MESSAGE,
)
from call_manager.proposal_assist.ports.errors import (
    CommitMutexTimeout as CommitMutexTimeout,
)


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


# The cap left in force for the REST of a mutex-held transaction, applied by
# acquire_commit_mutex once it has won the lock. PostgreSQL's own default is 0 =
# disabled, and this line used to restore exactly that — which is why every lock
# wait after the mutex was unbounded while the transaction held the mutex every
# other writer for that workspace needs.
#
# Why 2: `lock_timeout` is a PER-ACQUISITION cap, and a mutex-held transaction may
# wait on up to MAX_CONTENDED_ROWS = 2 distinct rows, against the two-thirds of
# the DELETE route's asserted 6.0s budget that lock stalls may consume (4.0s).
# 2 x 2 = 4.0. tests/proposal_assist/test_row_lock_ceilings.py enforces that
# arithmetic and parses the budget out of the DELETE test rather than restating
# it, so this number cannot drift away from its derivation silently.
#
# It matches every other mutex-held cap in the tree (row_locks.py's three,
# analysis/queue.py:41, phases/setup.py:136), so the six workspace_write routes
# keep setting the same value they already set.
MUTEX_TAIL_LOCK_TIMEOUT_SECONDS = 2


class WorkspaceDeleted(RuntimeError):
    """The workspace was soft-deleted, observed UNDER the commit mutex.

    Raised by :func:`acquire_commit_mutex_live` before the caller mutates
    anything, so the caller's transaction can be rolled back having written no
    git commit and no DB row. HTTP surfaces map this to 404 (the workspace is
    already 404 to every reader); agent tools return an error; background and
    teardown paths skip.
    """

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"workspace {workspace_id} was deleted")
        self.workspace_id = workspace_id


def is_lock_timeout(exc: BaseException) -> bool:
    """True iff *exc* is a Postgres lock_not_available (SQLSTATE 55P03) — a
    `SET LOCAL lock_timeout` that fired.

    Since Slice C3 the mutex-held tail keeps a 2s cap in force, so 55P03 is the
    routine "someone else holds a row I need" signal rather than a server fault.
    Every site that must tell contention from a real database failure calls this,
    so the SQLSTATE check exists exactly once.

    psycopg exposes the code as `sqlstate`; older drivers use `pgcode`. Anything
    that is not a DBAPI error at all (no `.orig`) is not a lock timeout.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "55P03"


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
        if is_lock_timeout(exc):
            raise CommitMutexTimeout(str(workspace_id)) from exc
        raise
    # Leave a BOUND in force for the rest of the transaction, rather than the
    # DEFAULT (= 0 = disabled) this used to restore. Only reachable on success —
    # after a timeout the txn is aborted and this statement would itself fail.
    #
    # This is Slice C3's entire mechanism. Restoring DEFAULT here un-bounded every
    # lock wait at all 27 mutex call sites, while the transaction held the mutex
    # every other writer for that workspace needs; one stuck row lock blocked the
    # owner's DELETE indefinitely. Replacing rather than deleting the statement is
    # what makes it nesting-proof: checkpoint_artifact re-acquires this same mutex
    # mid-tail (a re-entrant no-op for the lock), and with a restore it wiped its
    # caller's bound — with a re-applied cap there is nothing left to wipe.
    #
    # The resulting 55P03 is mapped to 429 + Retry-After: 2 by main.py's
    # OperationalError handler, centrally, because two sites already commit
    # outside their try block and a local clause would miss them.
    #
    # Precedence when nested inside bounded_lock_waits (row_locks.py): the
    # INNERMOST acquire_commit_mutex call always wins, because this statement
    # re-applies MUTEX_TAIL_LOCK_TIMEOUT_SECONDS unconditionally rather than
    # only when it would tighten the existing SET LOCAL. That can WIDEN a
    # shorter enclosing bounded_lock_waits cap back to this constant (e.g. a
    # 1s bounded_lock_waits scope containing a nested acquire ends up back at
    # 2s for the rest of that scope). Harmless today, since every production
    # bounded_lock_waits call already passes 2 — and strictly better than the
    # old behaviour, which widened all the way to DEFAULT (disabled).
    session.execute(text(f"SET LOCAL lock_timeout = '{MUTEX_TAIL_LOCK_TIMEOUT_SECONDS * 1000}ms'"))


def acquire_commit_mutex_live(
    session: Session, workspace_id: uuid.UUID, *, timeout_seconds: int = 10
) -> None:
    """Acquire the commit mutex, then refuse a workspace that is already deleted.

    The linearization point for mutex-participating writers: soft_delete takes
    this SAME mutex before it mutates anything, so a writer that was blocked on
    the mutex while a deletion committed observes the deletion the instant it
    wins the lock — and raises BEFORE performing any git or DB mutation.

    The re-read MUST be fresh. ``session.get(Workspace, id)`` can return the
    identity-map instance the caller loaded before blocking on the mutex, which
    is precisely the stale state this guard exists to defeat; a scalar SELECT of
    the column always round-trips. A missing row is treated as deleted (a missing
    workspace is never writable).

    Raises :class:`CommitMutexTimeout` exactly as :func:`acquire_commit_mutex`
    does, or :class:`WorkspaceDeleted`. Mutates nothing in either case; the
    caller rolls back.
    """
    acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)
    row = session.execute(
        select(Workspace.soft_deleted_at).where(Workspace.id == workspace_id)
    ).first()
    if row is None or row[0] is not None:
        raise WorkspaceDeleted(workspace_id)
