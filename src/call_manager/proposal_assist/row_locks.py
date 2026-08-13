"""SET LOCAL lock_timeout over an ENTIRE transaction — the one mechanism this
codebase uses to keep a transaction that holds the `workspaces` row from pinning
it against a concurrent DELETE.

Why this has to exist at all: PostgreSQL's `lock_timeout` defaults to 0
(disabled) and `db/session.py` passes no `connect_args`, so a plain row-lock wait
is unbounded. A DELETE that has already won the per-workspace commit mutex
executes `SET LOCAL lock_timeout = DEFAULT` (drafting/locking.py) before it
stamps `soft_deleted_at`, so there is no bound on the delete's side either: an
unbounded wait ANYWHERE inside a transaction that holds the workspace row blocks
the owner's DELETE for exactly that long, while the DELETE holds the advisory
mutex and every other mutex-participating writer for that workspace 429s behind
it — and the route's asserted budget (< 6.0s) is blown without limit.

The implementation is the idiom `acquire_commit_mutex` uses (SET LOCAL plus the
55P03 → typed-exception translation), minus its restore-to-DEFAULT: the callers
here hold rows from their first acquisition until COMMIT, and their DML, flush
and commit can each block on a lock, so restoring DEFAULT after the acquisitions
would leave exactly the stretch that matters unbounded. Nothing needs restoring —
SET LOCAL is scoped to the transaction, so a commit, a rollback or the implicit
abort a timeout causes all clear it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

# The four proposal-assist pass prefixes (coherence, evaluator, uncited-claims,
# review) take the workspace row FOR UPDATE and hold it across context assembly —
# which reads files out of workspaces_dir — and a job-row UPDATE, then commit.
# They hold no commit mutex, so nothing else bounds them.
#
# Why 2 seconds: the same derivation as phases/setup.py's
# _ROW_LOCK_TIMEOUT_SECONDS. The DELETE route's asserted budget is < 6.0s and the
# retirement barrier can consume most of it on its own, so a prefix's worst-case
# contribution has to be a small fraction of that budget rather than a multiple
# of it. Losing a race to contention is cheap on this path: the prefix has
# already claimed a job row that stays 'running', and the per-pass reaper
# reclaims it — a requeue, not a dead letter.
PASS_PREFIX_LOCK_TIMEOUT_SECONDS = 2


class RowLockTimeout(RuntimeError):
    """A transaction's SET LOCAL lock_timeout fired while waiting on a lock.

    Deliberately typed: lock contention says nothing about whether the workspace
    is gone, so callers must not route it to a 'cancelled'/'deleted' branch.
    """


class PassPrefixLockTimeout(RowLockTimeout):
    """Raised out of a pass prefix that could not take the workspace row within
    PASS_PREFIX_LOCK_TIMEOUT_SECONDS.

    It propagates: the prefix sits outside each runner's failure guard by design
    (the runner's docstrings say so — "a DB/file error there can propagate"), the
    scheduler's `_drain_pass_future` logs it, and the job is left 'running' for
    that pass's reaper. Giving up is the point — blocking here would pin the
    workspace row against the owner's DELETE.
    """


@contextmanager
def bounded_lock_waits(
    session: Session,
    *,
    timeout_seconds: float,
    error: type[RowLockTimeout] = RowLockTimeout,
    message: str = "timed out waiting for a row lock",
) -> Iterator[None]:
    """Bound EVERY lock wait in the enclosing transaction, not just the
    `with_for_update` acquisitions — see the module docstring for why the bound
    must extend past the acquisitions to the commit."""
    timeout_ms = int(timeout_seconds * 1000)
    session.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
    try:
        yield
    except OperationalError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if sqlstate == "55P03":
            raise error(message) from exc
        raise


@contextmanager
def bounded_prefix_lock_waits(session: Session) -> Iterator[None]:
    """The pass-prefix preset of `bounded_lock_waits`.

    Reads PASS_PREFIX_LOCK_TIMEOUT_SECONDS as a module global at call time so a
    test can shorten it with monkeypatch.
    """
    with bounded_lock_waits(
        session,
        timeout_seconds=PASS_PREFIX_LOCK_TIMEOUT_SECONDS,
        error=PassPrefixLockTimeout,
        message=(
            "timed out waiting for the workspace row — giving up rather than "
            "pinning it against a concurrent delete"
        ),
    ):
        yield
