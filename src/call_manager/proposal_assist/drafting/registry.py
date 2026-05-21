"""In-memory subprocess registry. Maps workspace_id → (AgentHandle,
last_touched_at, lock_session). Drives idle-timeout teardown."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from call_manager.proposal_assist.drafting.contract import (
    AgentHandle,
    DraftingAgent,
    StreamEvent,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    handle: AgentHandle
    last_touched_monotonic: float
    lock_session: Session | None  # held for the handle's lifetime
    in_flight: int = 0  # number of active send_turn iterations; never swept while > 0


class SubprocessRegistry:
    """One-instance-per-process registry of live drafting subprocesses.

    Concurrency: all public async methods serialize through a single
    asyncio.Lock. _pending_launches coalesces concurrent same-workspace
    launch() calls — the first caller becomes the launcher; subsequent
    callers await the same Future so the adapter is invoked exactly once.

    The lock also prevents the launch-vs-sweep race where a workspace is
    launched while sweep_idle is iterating.
    """

    def __init__(self, *, adapter: DraftingAgent, idle_seconds: int) -> None:
        self._adapter = adapter
        self._idle_seconds = idle_seconds
        self._entries: dict[uuid.UUID, _Entry] = {}
        # Futures that resolve to AgentHandle once a pending launch completes.
        # Keyed by workspace_id. Only populated between claim_launch_or_wait()
        # and fulfill_pending_launch() / fail_pending_launch().
        self._pending_launches: dict[uuid.UUID, asyncio.Future[AgentHandle]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Pending-launch coordination (Pattern B)
    # ------------------------------------------------------------------

    async def claim_launch_or_wait(
        self,
        workspace_id: uuid.UUID,
    ) -> tuple[bool, asyncio.Future[AgentHandle] | None]:
        """Determine whether the caller should drive the launch or await one.

        Returns ``(am_launcher, future_to_await)``.

        * ``am_launcher=True``:  the caller owns the launch.  It MUST call
          either :meth:`fulfill_pending_launch` on success or
          :meth:`fail_pending_launch` on failure — even if it raises.
          ``future_to_await`` is ``None`` in this case.

        * ``am_launcher=False``: another coroutine is already launching (or
          an entry already exists).  The caller should ``await
          future_to_await`` to get the handle.

        Single-loop only: the Future is created on the current running loop.
        All callers must share the same asyncio event loop (guaranteed for
        the FastAPI web layer).
        """
        async with self._lock:
            # Already cached — wrap the handle in a resolved future.
            existing = self._entries.get(workspace_id)
            if existing is not None:
                existing.last_touched_monotonic = time.monotonic()
                done: asyncio.Future[AgentHandle] = asyncio.get_running_loop().create_future()
                done.set_result(existing.handle)
                return (False, done)

            # Another coroutine is already launching — return its future.
            pending = self._pending_launches.get(workspace_id)
            if pending is not None:
                return (False, pending)

            # We are the launcher — install a new future and return.
            new_future: asyncio.Future[AgentHandle] = asyncio.get_running_loop().create_future()
            self._pending_launches[workspace_id] = new_future
            return (True, None)

    async def fulfill_pending_launch(
        self,
        workspace_id: uuid.UUID,
        handle: AgentHandle,
        *,
        lock_session: Session | None,
    ) -> None:
        """Called by the launcher on success.  Installs the entry and resolves
        any waiting futures.

        If a waiter's future was cancelled before this is called, the result
        is still set on it (set_result on a cancelled future is silently
        dropped — we check done() first to avoid the exception).
        """
        async with self._lock:
            future = self._pending_launches.pop(workspace_id, None)
            self._entries[workspace_id] = _Entry(
                handle=handle,
                last_touched_monotonic=time.monotonic(),
                lock_session=lock_session,
            )
        if future is not None and not future.done():
            future.set_result(handle)

    async def fail_pending_launch(
        self,
        workspace_id: uuid.UUID,
        exc: BaseException,
    ) -> None:
        """Called by the launcher on failure.  Propagates the exception to any
        waiting futures so they raise rather than hanging forever.

        After this call, the next ``claim_launch_or_wait`` for the same
        workspace_id will elect a new launcher (the old entry is gone).
        """
        async with self._lock:
            future = self._pending_launches.pop(workspace_id, None)
        if future is not None and not future.done():
            future.set_exception(exc)

    # ------------------------------------------------------------------
    # In-flight accounting
    # ------------------------------------------------------------------

    async def touch(self, workspace_id: uuid.UUID) -> None:
        """Mark a handle as just-used (so it doesn't get swept). Called
        by the controller after each turn completes."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is not None:
                entry.last_touched_monotonic = time.monotonic()

    async def increment_in_flight(self, workspace_id: uuid.UUID) -> None:
        """Signal that a send_turn iteration has started for this workspace.
        Prevents sweep_idle from terminating an active handle."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is not None:
                entry.in_flight += 1
                entry.last_touched_monotonic = time.monotonic()

    async def decrement_in_flight(self, workspace_id: uuid.UUID) -> None:
        """Signal that a send_turn iteration has finished (or been abandoned).
        Also bumps last_touched_monotonic so the handle's idle clock resets."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is not None:
                entry.in_flight = max(0, entry.in_flight - 1)
                entry.last_touched_monotonic = time.monotonic()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def terminate(self, workspace_id: uuid.UUID) -> None:
        """Explicitly tear down a workspace's handle. No-op if unknown.

        Order: terminate the adapter subprocess FIRST, then release the
        advisory lock.  This prevents another caller from acquiring the lock
        and launching a fresh subprocess while the old one is still
        disconnecting.  The trade-off: if adapter.terminate hangs, the lock is
        pinned until the hang clears — that's preferable to the lock leaking."""
        async with self._lock:
            entry = self._entries.pop(workspace_id, None)
        if entry is None:
            return
        try:
            await self._adapter.terminate(entry.handle)
        finally:
            if entry.lock_session is not None:
                from call_manager.proposal_assist.drafting.locking import (
                    release_workspace_lock,
                )

                release_workspace_lock(entry.lock_session, workspace_id)
                entry.lock_session.close()

    async def sweep_idle(self) -> int:
        """Terminate any handle untouched for >= idle_seconds. Returns
        the count of swept handles. Intended for periodic scheduler
        invocation."""
        now = time.monotonic()
        cutoff = now - self._idle_seconds

        async with self._lock:
            stale_ids = [
                wsid
                for wsid, entry in self._entries.items()
                if entry.last_touched_monotonic <= cutoff and entry.in_flight == 0
            ]
            stale_entries = [(wsid, self._entries.pop(wsid)) for wsid in stale_ids]

        # Terminate outside the lock — adapter.terminate may take a while
        # (subprocess teardown) and we don't want to block claim_launch_or_wait.
        # Order: terminate the adapter FIRST, then release the advisory lock.
        # This prevents another caller from acquiring the lock while the old
        # subprocess is still disconnecting (the same ordering as terminate()).
        for wsid, entry in stale_entries:
            try:
                await self._adapter.terminate(entry.handle)
            except Exception:
                logger.exception("sweep_idle: adapter.terminate raised for workspace %s", wsid)
            finally:
                if entry.lock_session is not None:
                    try:
                        from call_manager.proposal_assist.drafting.locking import (
                            release_workspace_lock,
                        )

                        release_workspace_lock(entry.lock_session, wsid)
                        entry.lock_session.close()
                    except Exception:
                        logger.exception(
                            "sweep_idle: failed to release lock for workspace %s", wsid
                        )
        return len(stale_entries)

    async def send_turn(
        self,
        handle: AgentHandle,
        user_message: str | list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        """Pass-through to the adapter's send_turn. The controller calls
        touch() after the iterator is fully consumed."""
        async for event in self._adapter.send_turn(handle, user_message):
            yield event
