"""Process-global registry of in-flight analysis passes, so a workspace DELETE can
cancel them.

Mirrors how collab exposes MANAGER: one module-level singleton, wired into the
retirement coordinator at startup (main.py). Tracking happens INSIDE each runner
rather than at the scheduler tick, so every caller is covered — not only the
scheduled path.

There is deliberately no in-memory tombstone here: "refuse to CLAIM a deleted
workspace" already exists durably in SQL. This registry answers the other
question — what is ALREADY running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Per entry: cancel the task, then await BOTH its exit AND the adapter's retirement
# of any generation it still has open — concurrently, under this one bound.
#
# Why one bound and not two: adapter teardown routes through _retire_generation,
# which does `await asyncio.shield(record.teardown)`. Under cancellation the shield
# delivers CancelledError to the awaiter while teardown keeps running, so awaiting
# the task's exit proves nothing about teardown. The adapter's own worst case is
# disconnect_deadline_seconds (3.0) + reap_deadline_seconds (2.0) = 5.0. A 5.0
# outer bound around a 5.0 inner one is a coin flip; 5.5 clears it and stays under
# _RETIREMENT_BARRIER_SECONDS (6.0).
_PASS_RETIRE_SECONDS = 5.5


@dataclass(eq=False)  # identity semantics — entries live in a set
class Entry:
    """One live pass task.

    The adapter is bound LATER, not at construction, because the entry must exist
    before the runner's prefix runs: an entry registered after the prefix is
    invisible to a DELETE that commits during it.

    There is no handle here on purpose. `launch` registers its generation BEFORE
    `client.connect()` and, on cancellation, retires it and re-raises without ever
    returning a handle — so a handle-keyed registry is blind to precisely that
    case. Confirmation is asked of the adapter instead.
    """

    workspace_id: uuid.UUID
    task: asyncio.Task
    adapter: object | None = field(default=None, repr=False)

    def bind_adapter(self, adapter: object) -> None:
        self.adapter = adapter

    def open_generations(self) -> int:
        """How many generations this entry's adapter still has un-confirmed-dead.

        Called DIRECTLY, not through getattr(..., None): a defensive fallback would
        make deleting this method from the real adapter invisible to every test.
        """
        if self.adapter is None:
            return 0
        return len(self.adapter.unconfirmed_launch_ids())

    async def retire_adapter(self) -> None:
        if self.adapter is None:
            return
        await self.adapter.retire_unconfirmed()


class PassRegistry:
    """What is running, and how to stop it."""

    def __init__(self) -> None:
        self._live: dict[uuid.UUID, set[Entry]] = {}
        self._unconfirmed: list[Entry] = []

    @contextlib.asynccontextmanager
    async def track(self, workspace_id: uuid.UUID) -> AsyncIterator[Entry]:
        entry = Entry(workspace_id=workspace_id, task=asyncio.current_task())
        self._live.setdefault(workspace_id, set()).add(entry)
        try:
            yield entry
        finally:
            # Removal is NOT discard. An entry whose adapter still has an open
            # generation is TRANSFERRED — on EVERY exit path, not only
            # retirement-driven ones.
            self._forget(entry)
            if entry.open_generations():
                self._unconfirmed.append(entry)
                logger.warning(
                    "passes.generation_unconfirmed",
                    extra={
                        "event": "pass_generation_unconfirmed",
                        "workspace_id": str(workspace_id),
                    },
                )

    def _forget(self, entry: Entry) -> None:
        bucket = self._live.get(entry.workspace_id)
        if bucket is None:
            return
        bucket.discard(entry)
        if not bucket:
            self._live.pop(entry.workspace_id, None)

    def live_count(self, workspace_id: uuid.UUID) -> int:
        return len(self._live.get(workspace_id, ()))

    def unconfirmed_count(self) -> int:
        return len(self._unconfirmed)

    async def retire_workspace(self, workspace_id: uuid.UUID) -> int:
        """Cancel every live pass for this workspace, CONCURRENTLY.

        Concurrency keeps the hook's cost independent of entry count.
        """
        entries = list(self._live.get(workspace_id, ()))
        if not entries:
            return 0
        await asyncio.gather(*(self._retire_entry(e) for e in entries))
        return len(entries)

    async def _retire_entry(self, entry: Entry) -> None:
        entry.task.cancel()
        waits = [asyncio.shield(_swallow(entry.task)), entry.retire_adapter()]
        try:
            await asyncio.wait_for(
                asyncio.gather(*waits, return_exceptions=True),
                timeout=_PASS_RETIRE_SECONDS,
            )
        except TimeoutError:  # asyncio.TimeoutError is an alias of this on 3.11+
            logger.warning(
                "passes.retire_timeout",
                extra={
                    "event": "pass_retire_timeout",
                    "workspace_id": str(entry.workspace_id),
                },
            )

    async def reconcile_unconfirmed(self) -> int:
        """Retry adapter retirement for generations never confirmed dead.

        A remediation ATTEMPT each tick, not a guarantee — a SIBLING of
        reconcile_live_clients, which covers a different population.

        Two properties this method must not lose:

          * the list is mutated IN PLACE, never rebound. Rebinding
            (`self._unconfirmed = still`) publishes a list assembled from a
            snapshot, so an entry that track()'s `finally` appends WHILE a
            reconcile is in flight is silently discarded and never retried again.
          * runs are deliberately NOT serialized — no lock here, of either
            flavour. Overlap is reachable: scheduler.py's tick abandons its
            future at `fut.result(timeout=30)` while the coroutine keeps running
            on app_loop, so a retire_unconfirmed() that never returns is a state
            this method can be left in. A lock taken by that run would never be
            released, which turns one wedged run into a permanent outage of
            reconciliation — every later tick either queues behind a lock nobody
            will release or, as a try-lock, declines outright, and no unconfirmed
            generation is ever retried again. Without one, each tick makes a
            fresh attempt from the head of the list.

            Concurrent runs are safe to allow: retire_unconfirmed routes through
            the adapter's _retire_generation, so a second caller JOINS the single
            teardown task rather than spawning a second kill
            (claude_adapter.py:667), and the removal below is idempotent.
        """
        if not self._unconfirmed:
            return 0
        acted = 0
        for entry in list(self._unconfirmed):
            acted += 1
            try:
                await entry.retire_adapter()
            except Exception:
                logger.exception(
                    "passes.reconcile_retire_failed",
                    extra={"workspace_id": str(entry.workspace_id)},
                )
            if not entry.open_generations():
                # Identity removal (Entry is eq=False), and suppressed because
                # a concurrent path may already have dropped it.
                with contextlib.suppress(ValueError):
                    self._unconfirmed.remove(entry)
        return acted


async def _swallow(task: asyncio.Task) -> None:
    """Await a task, absorbing its outcome — retirement cares only that it is
    finished."""
    with contextlib.suppress(BaseException):
        await task


PASS_REGISTRY = PassRegistry()
