"""Epoch fencing — the three primitives every belief-carrying actor uses.

Spec: docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md (revision 6),
and docs/superpowers/specs/2026-08-18-fence-session-ownership-design.md (revision
8) for the mechanism table that says which primitives an effect combines.

THE INVARIANT: a semantic effect produced from a belief formed earlier must
carry the lifecycle generation of that belief and validate it, durably in
Postgres, at the moment the effect takes place.

Three primitives. Which ones an effect uses is decided ONCE per operation and
never changed mid-flight (2026-08-18 spec 3.5):

  A. `live_at_generation` — a PREDICATE, no lock. Goes in the WHERE of the
     statement that performs the effect, so the fence and the write are ONE
     statement. NOT SUFFICIENT ALONE: it takes no lock on the workspace row, so
     under READ COMMITTED it evaluates against the statement's snapshot and a
     transition committing after that snapshot is invisible to it.

  C. `acquire_shared_lease` — the workspace's commit-mutex key in SHARED mode.
     Pairs with A for every DB-only effect, acquired FIRST. Shared locks do not
     conflict, so effects stay concurrent with each other; a lifecycle
     transition needs the key exclusively, so it either waits for in-flight
     effects or wins first and leaves A to fail.

  B. `acquire_commit_mutex_at_generation` — the EXCLUSIVE mutex plus an
     under-mutex re-read. For effects spanning git, the filesystem, or multiple
     rows that must move together, taken before the first effect of any kind.

NEVER take C and then B for the same workspace. Two operations each holding
shared and each waiting to upgrade is the one deadlock shape this protocol has.

Where a transaction ALREADY holds the WORKSPACE row under FOR UPDATE, use none
of them: assert the generation on that already-locked row. Only the workspace row
qualifies — a job row carries no lifecycle state, so locking one proves nothing.
Acquiring the mutex afterwards inverts the lock order and opens a real Postgres
40P01 against the delete path (spec 5).

LOCK ORDER, per job table: the mutex (M) first, always. Then the workspace row
(W) BEFORE every job table — coherence, evaluator, review, uncited-claims and
turn jobs — because the delete transaction updates the workspace row before it
cancels those (measured, review 2026-09-02 A10 F2). The ONE exception is
`workspace_setup_jobs`: J then W, because the rubric `activate` route locks the
job row before it flushes its workspace update and the delete never locks setup
job rows at all (phases/setup.py _m3 docstring).

`WorkspaceGenerationStale` is DEFINED in ports/errors.py and re-exported here.
It moved in F3b: a tool has to be able to CATCH it, and this module imports
sqlalchemy and db.models, so under the F3c import boundary no tool could name it.
Every pre-F3b import site keeps working through the re-export.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import (
    MUTEX_TAIL_LOCK_TIMEOUT_SECONDS,
    CommitMutexTimeout,
    WorkspaceDeleted,
    acquire_commit_mutex,
    commit_mutex_key,
    is_lock_timeout,
)
from call_manager.proposal_assist.ports.errors import WorkspaceGenerationStale

__all__ = [
    "WorkspaceGenerationStale",
    "acquire_commit_mutex_at_generation",
    "acquire_shared_lease",
    "live_at_generation",
]


def live_at_generation(workspace_id: uuid.UUID, generation: int):
    """Primitive A. "Is this workspace LIVE and STILL at *generation*?", as a
    predicate that governs the WRITE.

    NOT SUFFICIENT ALONE. This is an exists() subquery and it takes NO LOCK on the
    workspace row, so under READ COMMITTED it evaluates against the statement's
    snapshot and a transition committing after that snapshot is invisible to it.
    Pair it with `acquire_shared_lease` (Primitive C), which must be acquired
    FIRST, in the same transaction:

        acquire_shared_lease(session, ws_id)
        session.execute(
            update(SomeJob)
            .where(SomeJob.id == job_id, live_at_generation(ws_id, gen))
            .values(status="done")
        )

    NOT as a separate check before the write. A predicate evaluated in its own
    statement does not guard a later one: both run in a single READ COMMITTED
    transaction and each takes a FRESH snapshot, so a transition committing
    between them is invisible to the check and fully effective on the write.
    That defect shipped twice (fixed in 28abbc4 and again in 701fbb7).

    NOT INTERCHANGEABLE with `jobs_retirement._still_at_generation`, which
    deliberately omits the liveness clause: it runs during the delete fan-out,
    when `soft_deleted_at` is already stamped, so it must match a DELETED
    workspace. Adding liveness there would make every cancel a no-op and
    silently disarm the retirement fan-out. The two predicates look like
    duplicates and are opposites. Do not merge them.

    Generation is compared for EQUALITY. A generation lower than the current one
    is impossible under correct operation, so `>=` would wave through exactly the
    corruption this exists to detect. Note that 0 is a real generation:
    `Workspace.lifecycle_generation` has server_default 0, so a fresh workspace
    IS at generation 0 and must match.
    """
    return exists().where(
        Workspace.id == workspace_id,
        Workspace.soft_deleted_at.is_(None),
        Workspace.lifecycle_generation == generation,
    )


def acquire_commit_mutex_at_generation(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    generation: int,
    timeout_seconds: int = 10,
) -> None:
    """Primitive B. Acquire the commit mutex, then refuse a workspace that is
    deleted or has moved past *generation*.

    For effects spanning git, the filesystem, or multiple rows that must move
    together — and for those it is MANDATORY, taken before the first effect of
    any kind. For DB-only effects use `acquire_shared_lease` plus
    `live_at_generation` instead: those stay concurrent with each other, while
    this mutex serializes every writer for the workspace.

    Never call this while already holding the shared lease for the same
    workspace. Two operations each holding shared and each waiting to upgrade is
    the one deadlock shape this protocol has.

    LOCK ORDER: this acquires the mutex (M). It must be called BEFORE any setup
    job (J) or workspace (W) row lock — M first, then W before every job table
    except workspace_setup_jobs (see the module docstring). Calling it after a job or
    workspace row lock inverts the order against the delete path and opens a
    real Postgres 40P01. Where the caller ALREADY holds the workspace row under
    FOR UPDATE, do not call this at all — assert the generation on that
    already-locked row.

    Never hold this across context assembly or an LLM call.

    Both columns come from ONE statement, and it is a scalar SELECT rather than
    `session.get()`. `session.get()` can return the identity-map instance the
    caller loaded BEFORE blocking on the mutex, which carries the pre-race
    generation — precisely the staleness this guard exists to defeat. That
    defect shipped in Slice C1 and was fixed in 701fbb7.

    Raises CommitMutexTimeout exactly as acquire_commit_mutex does,
    WorkspaceDeleted if the workspace is gone or deleted, or
    WorkspaceGenerationStale if it is live at a different generation. Mutates
    nothing in every case; the caller rolls back.

    Deleted takes precedence over stale: a deletion also bumps the generation, so
    both conditions hold at once, and callers map them to different outcomes
    (404 versus a quiet abandon-with-accounting).
    """
    acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)
    row = session.execute(
        select(Workspace.soft_deleted_at, Workspace.lifecycle_generation).where(
            Workspace.id == workspace_id
        )
    ).first()
    if row is None or row[0] is not None:
        raise WorkspaceDeleted(workspace_id)
    if row[1] != generation:
        raise WorkspaceGenerationStale(workspace_id, expected=generation, actual=row[1])


def acquire_shared_lease(
    session: Session, workspace_id: uuid.UUID, *, timeout_seconds: int = 10
) -> None:
    """Primitive C. Take the workspace's commit-mutex key in SHARED mode for the
    rest of this transaction, so a lifecycle transition cannot commit underneath
    the effect that follows.

    Pair it with `live_at_generation` in the WHERE of the effect's single write
    (spec 3.5, mechanism 1). The predicate alone is NOT enough: it is an exists()
    subquery taking no lock on the workspace row, so under READ COMMITTED it
    evaluates against the statement's snapshot and a transition committing after
    that snapshot is invisible to it. Reproduced on PostgreSQL 16 with an INSERT,
    which has no target row and therefore no re-check to mask the race.

    The lease is what turns "I checked" into "I waited, then checked": shared
    locks do not conflict, so effects stay concurrent with each other, while a
    lifecycle transition needs the key EXCLUSIVELY and therefore either waits for
    in-flight effects or wins first and leaves the effect's predicate to fail.
    That concurrency holds only while no lifecycle transition is pending:
    PostgreSQL's anti-starvation queueing puts a new shared request behind an
    already-waiting exclusive one, so once a transition is queued, further
    effects queue behind it too rather than passing it.

    NEVER upgrade: an operation holding this lease must not go on to call
    acquire_commit_mutex* for the same workspace. Two operations each holding
    shared and each waiting to upgrade is the one deadlock shape this protocol
    has. An operation that will touch git or the filesystem takes the EXCLUSIVE
    mutex first, before its first effect of any kind.

    Transaction scoped: released at COMMIT or ROLLBACK, with no explicit unlock.
    Requires a non-AUTOCOMMIT session — an AUTOCOMMIT connection would release it
    immediately and silently provide no exclusion.

    Bounded exactly as acquire_commit_mutex is, and it leaves the same
    MUTEX_TAIL_LOCK_TIMEOUT_SECONDS cap in force for the rest of the transaction
    (Slice C3) rather than restoring DEFAULT, which is 0 = disabled.
    """
    timeout_ms = int(timeout_seconds) * 1000
    if timeout_ms <= 0:
        raise ValueError(f"timeout_seconds must be a positive int, got {timeout_seconds!r}")
    session.execute(text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
    try:
        session.execute(
            text("SELECT pg_advisory_xact_lock_shared(:k)"),
            {"k": commit_mutex_key(workspace_id)},
        )
    except OperationalError as exc:
        if is_lock_timeout(exc):
            raise CommitMutexTimeout(str(workspace_id)) from exc
        raise
    session.execute(text(f"SET LOCAL lock_timeout = '{MUTEX_TAIL_LOCK_TIMEOUT_SECONDS * 1000}ms'"))
