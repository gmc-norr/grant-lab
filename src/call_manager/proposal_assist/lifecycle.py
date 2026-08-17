"""The single authority on whether a workspace is retired.

Retention Slice C0 (spec 4.5). Before this, four retirement hooks each kept
their own in-memory tombstone and a reactivation had to clear every one of
them — a fan-out that can half-succeed, which is why revision 6's design needed
a "retryable clearing" contract it could not honour.

One authority removes that class. `transition` is MONOTONIC: an effect from a
generation at or below the one already applied is refused. That is what makes
arrival order irrelevant, and arrival order genuinely is out of our control —
the jobs hook's work escapes every lock this process holds (spec 4.6).

`transition` is a context manager because its caller's WORK has to happen inside
the decision. WorkspaceRetirement holds it across the whole hook fan-out, so a
restore cannot land between "this retirement is current" and the teardown that
conclusion authorises. The consequence, which every hook and guard depends on:
NOTHING called from inside that body may take this lock again — `is_retired` is
lock-free precisely so guards can be called from there.

`is_retired` is also synchronous because it is called on every collab write;
making those await a lock would put a scheduling point in the hot path for a
dictionary lookup.

Growth: one entry per workspace transitioned per process, the same footprint as
the two tombstone sets it replaces. Eviction stays a later candidate (it was one
before this slice too), not built here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _State:
    generation: int
    retired: bool


class WorkspaceLifecycle:
    def __init__(self) -> None:
        self._state: dict[uuid.UUID, _State] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    def is_retired(self, workspace_id: uuid.UUID) -> bool:
        """Synchronous, and safe without the lock: a dict read of a single
        reference under the GIL. A caller can observe the state either side of a
        concurrent transition, which is exactly as true of any lock-free guard —
        the durable backstop is the under-mutex re-read of soft_deleted_at.

        Lock-free is also a REQUIREMENT, not just an optimisation: guards run
        inside hooks that the coordinator invokes while holding this lock."""
        s = self._state.get(workspace_id)
        return s.retired if s is not None else False

    def applied_generation(self, workspace_id: uuid.UUID) -> int:
        """0 means no transition has ever been applied. Every transition
        allocates >= 1, so 0 can never collide with a real generation."""
        s = self._state.get(workspace_id)
        return s.generation if s is not None else 0

    @contextlib.asynccontextmanager
    async def transition(
        self, workspace_id: uuid.UUID, *, generation: int, retired: bool
    ):
        """Decide whether the transition that allocated *generation* is current,
        and hold this workspace's lock for the caller's whole body either way.

        Yields True if it took effect, False if it was superseded. `<=`, not
        `<`: re-delivering the SAME generation must not flip state, or a
        duplicate delivery could undo its own transition.

        The lock is held on BOTH paths. Releasing early on the superseded path
        would let a stale caller interleave its work with a live transition's,
        which is the whole thing this serialises.
        """
        lock = self._locks.get(workspace_id)
        if lock is None:
            lock = self._locks[workspace_id] = asyncio.Lock()
        async with lock:
            current = self._state.get(workspace_id)
            if current is not None and generation <= current.generation:
                yield False
                return
            self._state[workspace_id] = _State(generation=generation, retired=retired)
            yield True

    async def apply(
        self, workspace_id: uuid.UUID, *, generation: int, retired: bool
    ) -> bool:
        """`transition` with an empty body, for callers with no contained work
        (C1's restore, and tests). Deliberately delegates rather than repeating
        the check: one mutator, one place monotonicity lives."""
        async with self.transition(
            workspace_id, generation=generation, retired=retired
        ) as took_effect:
            return took_effect


# The process-wide authority. A module singleton for the same reason MANAGER and
# PASS_REGISTRY are: the subsystems that consult it are themselves module
# singletons constructed at import. Production construction sites inject this
# object EXPLICITLY (main.py, collab/room.py, drafting/factory.py) so the wiring
# is readable; it is also the constructor DEFAULT, so that a construction site
# someone forgets to update binds to the one authority rather than quietly
# minting a second one. Tests that want isolation pass their own instance.
LIFECYCLE = WorkspaceLifecycle()


# Upper bound on how long the sync restore route blocks waiting for the apply.
#
# MUST NOT be smaller than retirement._RETIREMENT_BARRIER_SECONDS. A restore
# racing a retirement waits for the per-workspace lifecycle lock, which the
# retirement coordinator holds across its ENTIRE hook fan-out (C0). A tighter
# bound here would turn that ordinary race into a 503 for the admin.
# test_restore_bridge.py asserts the inequality against the live constants.
_RESTORE_BARRIER_SECONDS = 6.0


def signal_workspace_restored(request, *, workspace_id: uuid.UUID, generation: int) -> bool:
    """Route-side (SYNC): apply `active` on the app loop and BLOCK (bounded).

    Returns True if the apply landed within the bound, False if it could not be
    scheduled or timed out. The MIRROR of retirement.signal_workspace_retirement,
    with the opposite failure policy on purpose: retirement swallows everything
    because its delete has already committed and must not fail late, while
    restore's caller must answer 503 rather than promise a reactivation it could
    not confirm (spec 5.4).

    A timed-out apply is NOT cancelled. It keeps running on the loop and may land
    later, which is safe precisely because apply is monotonic: the late one, the
    retry, and any duplicate converge on the same state and whichever arrives
    second is ignored. That is also why there is no in-flight registry here.

    *generation* is the one the restoring transaction ALLOCATED. Never re-read
    one here (spec 4.4).
    """
    loop = getattr(request.app.state, "app_loop", None)
    lifecycle = getattr(request.app.state, "workspace_lifecycle", None)
    if loop is None or lifecycle is None:
        logger.warning(
            "restore.bridge_unavailable",
            extra={
                "event": "restore_bridge_unavailable",
                "workspace_id": str(workspace_id),
            },
        )
        return False

    coro = lifecycle.apply(workspace_id, generation=generation, retired=False)
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:
        coro.close()   # never-awaited coroutine -> close it before reporting
        logger.warning(
            "restore.schedule_failed",
            extra={
                "event": "restore_schedule_failed",
                "workspace_id": str(workspace_id),
            },
            exc_info=True,
        )
        return False
    try:
        fut.result(timeout=_RESTORE_BARRIER_SECONDS)
    except concurrent.futures.TimeoutError:
        logger.warning(
            "restore.barrier_timeout",
            extra={
                "event": "restore_barrier_timeout",
                "workspace_id": str(workspace_id),
                "generation": generation,
            },
        )   # deliberately NOT cancelled — see the docstring
        return False
    except Exception:
        logger.warning(
            "restore.apply_failed",
            extra={"event": "restore_apply_failed", "workspace_id": str(workspace_id)},
            exc_info=True,
        )
        return False
    return True
