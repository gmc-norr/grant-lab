"""Workspace-retirement coordinator + sync→loop bridge (Slice B-retire).

A neutral home so the turns registry and the collab RoomManager never import
each other: the app wires each subsystem's teardown as a hook at startup
(main.py), and this coordinator fans out to all of them when a workspace is
deleted. The bridge mirrors collab.permissions.signal_user_revocation — the
SYNC delete route reaches the async coordinator via the app loop, bounded so a
hanging hook can never wedge the route.

Hooks now receive the generation their retirement allocated (Slice C0). `jobs`
is the only one that uses it, because its work escapes the coordinator's gate
(Task 4 wires the SQL predicate). The remaining hooks ignore the parameter —
the coordinator's own gate (inside `transition`) ensures they are never invoked
for a superseded retirement.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid
from collections.abc import Awaitable, Callable

from grant_lab.proposal_assist.lifecycle import WorkspaceLifecycle

logger = logging.getLogger(__name__)

# A retirement hook: given a workspace_id and the generation its transaction
# allocated, tear that subsystem's live state down. Return value is IGNORED
# (RoomManager.retire_workspace returns an int count for testability; the
# coordinator discards it). `jobs` is the only hook that uses the generation;
# the others ignore it because the coordinator's gate covers them.
RetirementHook = Callable[[uuid.UUID, int], Awaitable[object]]

# Hooks that MUST be registered or the app must not boot: a missing one would
# mean a delete silently fails to stop that subsystem. Enforced at construction.
REQUIRED_HOOKS = ("turns", "collab", "jobs", "passes")

# Upper bound on how long the sync delete route blocks on the retirement barrier
# (mirrors permissions._REVOCATION_BARRIER_SECONDS). On overrun the route returns
# and retirement continues on the loop. For MUTEX-PARTICIPATING writers B4's
# under-mutex re-read of soft_deleted_at is the durable backstop; the collab
# client-write and cold-hydration paths are covered by their own guards (the
# room.lock tombstone and load_or_hydrate's mutex), NOT by B4.
#
# MUST EXCEED every hook's own worst case, or the barrier fires on healthy-but-slow
# deletes and the route returns 204 before retirement has actually finished tearing
# down live state — harmless today (the backstops above are what actually keep
# things correct), just a needless degrade-to-eventual. The budget was originally
# sized against a sharper hazard — scrub_workspace_bytes starting to delete files
# while this same teardown was still in flight — but Slice A's freeze-only delete
# no longer calls scrub_workspace_bytes on this path, so that race is gone here.
# It returns in Slice E's admin purge, where the barrier will have to be PROVEN
# against it rather than assumed the way this one is. Hooks run concurrently, so
# the budget is the MAX, not the sum:
#   collab: max over the two rooms (they retire concurrently) of
#           _RETIRE_LOCK_WAIT_SECONDS (1.0) + _RETIRE_OPS_DRAIN_SECONDS (2.0) = 3.0
#   turns:  adapter disconnect_deadline_seconds (3.0) + reap_deadline_seconds (2.0) = 5.0
#   jobs:   _JOBS_CANCEL_SECONDS (1.0), and every query is SKIP LOCKED so it
#           never waits on a live worker's row = 1.0
#   passes: _PASS_RETIRE_SECONDS (5.5) — INDEPENDENT of entry count, because
#           PassRegistry.retire_workspace gathers its entries concurrently. If
#           it ever retired serially this budget would become N x 5.5.
# => 5.5 worst case (passes), so 5.0 would be a coin flip. 6.0 keeps
# headroom. test_retirement_coordinator.py asserts this inequality against the live
# constants so raising any sub-bound fails loudly instead of silently eroding it.
_RETIREMENT_BARRIER_SECONDS = 6.0


class MissingRetirementHook(RuntimeError):
    """A required retirement hook was not registered at construction — a STARTUP
    invariant violation, raised so the app fails to boot rather than silently
    skipping a subsystem's teardown on every delete."""


class WorkspaceRetirement:
    """Fans a workspace deletion out to every registered subsystem hook.

    Construction validates REQUIRED_HOOKS; retire() applies the lifecycle
    transition and — only if it took effect — runs all hooks concurrently and
    in isolation, never raising."""

    def __init__(
        self, hooks: dict[str, RetirementHook], *, lifecycle: WorkspaceLifecycle
    ) -> None:
        missing = [name for name in REQUIRED_HOOKS if name not in hooks]
        if missing:
            raise MissingRetirementHook(
                f"missing required retirement hook(s): {', '.join(missing)}"
            )
        self._hooks = dict(hooks)
        self._lifecycle = lifecycle

    async def retire(self, workspace_id: uuid.UUID, generation: int) -> None:
        """Apply the retirement to the lifecycle authority, and — only if it is
        still current — run EVERY registered hook concurrently, each isolated in
        its own try/except so one hook's failure never suppresses another's.

        *generation* is the one the deleting transaction ALLOCATED, passed down
        untouched. Nothing here may re-read it: a generation read at this point
        would be the one current when teardown ran, not the one this teardown
        belongs to, so a transition that landed in between would look like this
        one (spec 4.4).

        The gate is HERE rather than in the hooks for two reasons. All four
        hooks carry the same generation, so a per-hook check would let the first
        win and leave the workspace half torn down. And the lifecycle lock is
        held across the whole fan-out — so a hook that took it would deadlock
        against its own caller. Inside this block, `is_retired` (lock-free) is
        the only lifecycle call that is legal.

        Holding the lock for the fan-out bounds nothing new: the hooks are
        individually bounded by the same budgets _RETIREMENT_BARRIER_SECONDS is
        derived from above (max 5.5s, passes), and test_retirement_coordinator.py
        asserts that inequality against the live constants.

        Never raises; return values are discarded.
        """
        async with self._lifecycle.transition(
            workspace_id, generation=generation, retired=True
        ) as took_effect:
            if not took_effect:
                logger.info(
                    "retirement.superseded",
                    extra={
                        "event": "retirement_superseded",
                        "workspace_id": str(workspace_id),
                        "generation": generation,
                    },
                )
                return

            async def _run(name: str, hook: RetirementHook) -> None:
                try:
                    await hook(workspace_id, generation)
                except Exception:
                    logger.warning(
                        "retirement.hook_failed",
                        extra={
                            "event": "retirement_hook_failed",
                            "hook": name,
                            "workspace_id": str(workspace_id),
                        },
                        exc_info=True,
                    )

            await asyncio.gather(*(_run(name, hook) for name, hook in self._hooks.items()))


def signal_workspace_retirement(
    request, *, workspace_id: uuid.UUID, generation: int
) -> None:
    """Route-side (SYNC): schedule WorkspaceRetirement.retire on the app loop and
    BLOCK (bounded) until it returns. Mirrors signal_user_revocation. NEVER raises
    into the route: a missing loop/coordinator, a schedule failure, a timeout, or
    a coordinator error all degrade to a logged warning — the DB soft-delete has
    already committed, so a delete must not fail because retirement could not be
    scheduled.

    *generation* is the value the deleting transaction allocated and the caller
    supplies. This function must NEVER read a generation: the row's current value
    at this point could reflect a restore that landed between the commit and this
    signal, making this delete's teardown look current (spec 4.4)."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        coordinator = getattr(request.app.state, "workspace_retirement", None)
        if loop is None or coordinator is None:
            return
        coro = coordinator.retire(workspace_id, generation)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()   # never-awaited coroutine → close it before re-raising
            raise
        try:
            fut.result(timeout=_RETIREMENT_BARRIER_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "retirement.barrier_timeout",
                extra={
                    "event": "retirement_barrier_timeout",
                    "workspace_id": str(workspace_id),
                },
            )   # retirement continues on the loop; degrade to eventual
    except Exception:
        logger.warning(
            "retirement.signal_failed",
            extra={
                "event": "retirement_signal_failed",
                "workspace_id": str(workspace_id),
            },
            exc_info=True,
        )
