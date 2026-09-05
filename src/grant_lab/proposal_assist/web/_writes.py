"""`workspace_write` — the transaction scope for a mutex-held workspace write.

Slice C2 put six owner-write routes under the per-workspace commit mutex and
required each of them to bound every lock wait in the transaction THROUGH
`session.commit()`. It expressed that as a six-step ceremony hand-written at six
call sites, and the ceremony was wrong at four of them before review caught it:
a bound that stops short of the commit leaves the write bounded but UNtranslated,
so a contended row answers 500 where the slice contracts for 429.

This manager exists so the rule cannot be written wrongly. A route that uses it
does not write a commit at all, so it cannot put the commit outside the bound.

WHAT IT DOES NOT DO, deliberately:
  - authorization. `require_owner` stays visible in the route, above the `with`.
    Slice C2 established that authorization precedes semantic validation which
    precedes any lock; folding authz in here would hide that ordering.
  - validation. Same reason. Note `add_member`/`remove_member` validate INSIDE
    the block on purpose — their validation is a DB read that must not run
    before the mutex.
  - row locks. The route writes its own `.with_for_update()`.
  - freshness re-reads. `activate`'s re-read must sit AFTER its setup-job lock
    to match `phases/setup.py`'s `_m3` (M -> J -> W); a parameter here would run
    it at the top of the block and invert that order, opening a real 40P01. The
    re-reads stay in the route bodies where their position is visible.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy.orm import Session

from grant_lab.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
)
from grant_lab.proposal_assist.fencing import acquire_commit_mutex_at_generation
from grant_lab.proposal_assist.ports.stamp import FenceStamp
from grant_lab.proposal_assist.row_locks import RowLockTimeout, bounded_lock_waits


def _busy() -> HTTPException:
    """The one spelling of the busy response. Byte-identical to the twelve
    hand-written copies this manager replaces."""
    return HTTPException(
        status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
    )


@contextmanager
def workspace_write(
    session: Session,
    *,
    stamp: FenceStamp,
    timeout_seconds: float,
    what: str,
) -> Iterator[None]:
    """Run the body as a mutex-held, fully bounded workspace write.

    Takes the commit mutex at the stamp's generation, opens `bounded_lock_waits`
    for the rest of the transaction, yields, and COMMITS INSIDE THE BOUND. Both
    timeout types become `429 + Retry-After: 2`. `WorkspaceDeleted` propagates
    untouched (main.py answers 404 — the documented straddle contract), as does
    an `HTTPException` raised by the body, which is what preserves
    `retry_setup`'s 404/409 and `activate`'s 409.

    Rolls back on EVERY exceptional exit. That is not redundant with the
    caller's `with session_factory()`: without it, a caller that catches the
    exception and reuses the session carries the body's pending writes into its
    next commit, and after a 55P03 the transaction is aborted and refuses
    further statements. The rollback also clears the `SET LOCAL lock_timeout`
    the bound deliberately leaves in force, which is correct — that setting is
    scoped to the transaction being ended.

    DO NOT pass `timeout_seconds` to `acquire_commit_mutex_at_generation` below,
    not even from a parameter defaulting to 10. Both the straddle harness and
    this manager's own tests shorten the mutex wait by replacing the module
    global with `functools.partial(..., timeout_seconds=1)`, and a call-time
    keyword OVERRIDES a partial's keyword — so passing it explicitly silently
    restores the production 10s and makes every wedged-mutex test wait ten
    seconds instead of one. Spec §3 states this as a requirement.
    """
    committed = False
    try:
        try:
            acquire_commit_mutex_at_generation(
                session, stamp.workspace_id, generation=stamp.generation
            )
        except CommitMutexTimeout as e:
            raise _busy() from e
        with bounded_lock_waits(
            session,
            timeout_seconds=timeout_seconds,
            message=f"timed out waiting for a row lock in the {what} transaction",
        ):
            yield
            session.commit()
            committed = True
    except RowLockTimeout as e:
        raise _busy() from e
    finally:
        if not committed:
            session.rollback()
