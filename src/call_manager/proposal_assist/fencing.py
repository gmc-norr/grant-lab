"""Epoch fencing — the two primitives every belief-carrying actor uses.

Spec: docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md (revision 3).

THE INVARIANT: a semantic effect produced from a belief formed earlier must
carry the lifecycle generation of that belief and validate it, durably in
Postgres, at the moment the effect takes place.

Two primitives, chosen by what the effect touches:

  A. `live_at_generation` — a PREDICATE, no lock. For effects that are purely DB
     rows. Drop it into the WHERE of the statement that performs the effect, so
     the fence and the write are ONE statement. This is the default: it cannot
     create a lock-order problem because it takes no lock.

  B. `acquire_commit_mutex_at_generation` — mutex + under-mutex re-read. For
     effects spanning git, the filesystem, or multiple rows that must move
     together.

Where a transaction ALREADY holds the workspace row under FOR UPDATE, use
neither: assert the generation on that already-locked row. Acquiring the mutex
afterwards inverts the normative M -> J -> W order and opens a real Postgres
40P01 against the delete path (spec 5).

`WorkspaceGenerationStale` lives here rather than beside `WorkspaceDeleted` in
drafting/locking.py because it is raised only by fencing code, and F2's coverage
gate keys on this module. The asymmetry is deliberate.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists

from call_manager.db.models import Workspace


class WorkspaceGenerationStale(RuntimeError):
    """The workspace has moved past the generation the caller carried.

    NOT an error condition. It means "your epoch is over; produce no further
    semantic effect." It never reaches a user as a 5xx.

    It does NOT mean "write nothing". An actor that catches this must still
    record cleanup and accounting — tokens, dollars, terminal status, releasing
    an active-unique slot — via conditional writes only (spec 7). Abandoning
    silently strands the job holding its slot and destroys deliberate late token
    accounting (see review/pass_runner.py, which keeps the tokens a cancelled
    run's sub-call spent on purpose).
    """

    def __init__(self, workspace_id: uuid.UUID, *, expected: int, actual: int) -> None:
        super().__init__(
            f"workspace {workspace_id} moved from generation {expected} to {actual}"
        )
        self.workspace_id = workspace_id
        self.expected = expected
        self.actual = actual


def live_at_generation(workspace_id: uuid.UUID, generation: int):
    """Primitive A. "Is this workspace LIVE and STILL at *generation*?", as a
    predicate that governs the WRITE.

    Use it INSIDE the statement that performs the effect:

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
