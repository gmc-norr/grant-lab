"""Workspace-retirement coordinator + sync→loop bridge (Slice B-retire).

A neutral home so the turns registry and the collab RoomManager never import
each other: the app wires each subsystem's teardown as a hook at startup
(main.py), and this coordinator fans out to all of them when a workspace is
deleted. The bridge mirrors collab.permissions.signal_user_revocation — the
SYNC delete route reaches the async coordinator via the app loop, bounded so a
hanging hook can never wedge the route.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# A retirement hook: given a workspace_id, tear that subsystem's live state down.
# Return value is IGNORED (RoomManager.retire_workspace returns an int count for
# testability; the coordinator discards it).
RetirementHook = Callable[[uuid.UUID], Awaitable[object]]

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
# deletes and scrub_workspace_bytes starts deleting files while teardown is still in
# flight. Hooks run concurrently, so the budget is the MAX, not the sum:
#   collab: max over the two rooms (they retire concurrently) of
#           _RETIRE_LOCK_WAIT_SECONDS (1.0) + _RETIRE_OPS_DRAIN_SECONDS (2.0) = 3.0
#   turns:  adapter disconnect_deadline_seconds (3.0) + reap_deadline_seconds (2.0) = 5.0
#   jobs:   _JOBS_CANCEL_SECONDS (1.0), and every query is SKIP LOCKED so it
#           never waits on a live worker's row = 1.0
#   passes: _PASS_RETIRE_SECONDS (5.5) — INDEPENDENT of entry count, because
#           PassRegistry.retire_workspace gathers its entries concurrently. If
#           it ever retired serially this budget would become N x 5.5.
# => 5.5 worst case (passes), so 5.0 would be a coin flip. 6.0 keeps headroom;
# headroom. test_retirement_coordinator.py asserts this inequality against the live
# constants so raising any sub-bound fails loudly instead of silently eroding it.
_RETIREMENT_BARRIER_SECONDS = 6.0


class MissingRetirementHook(RuntimeError):
    """A required retirement hook was not registered at construction — a STARTUP
    invariant violation, raised so the app fails to boot rather than silently
    skipping a subsystem's teardown on every delete."""


class WorkspaceRetirement:
    """Fans a workspace deletion out to every registered subsystem hook.

    Construction validates REQUIRED_HOOKS; retire() runs all hooks concurrently
    and in isolation and never raises."""

    def __init__(self, hooks: dict[str, RetirementHook]) -> None:
        missing = [name for name in REQUIRED_HOOKS if name not in hooks]
        if missing:
            raise MissingRetirementHook(
                f"missing required retirement hook(s): {', '.join(missing)}"
            )
        self._hooks = dict(hooks)

    async def retire(self, workspace_id: uuid.UUID) -> None:
        """Run EVERY registered hook concurrently, each isolated in its own
        try/except so one hook's failure never suppresses another's teardown.
        Never raises; return values are discarded."""

        async def _run(name: str, hook: RetirementHook) -> None:
            try:
                await hook(workspace_id)
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


def signal_workspace_retirement(request, *, workspace_id: uuid.UUID) -> None:
    """Route-side (SYNC): schedule WorkspaceRetirement.retire on the app loop and
    BLOCK (bounded) until it returns. Mirrors signal_user_revocation. NEVER raises
    into the route: a missing loop/coordinator, a schedule failure, a timeout, or
    a coordinator error all degrade to a logged warning — the DB soft-delete has
    already committed, so a delete must not fail because retirement could not be
    scheduled."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        coordinator = getattr(request.app.state, "workspace_retirement", None)
        if loop is None or coordinator is None:
            return
        coro = coordinator.retire(workspace_id)
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
