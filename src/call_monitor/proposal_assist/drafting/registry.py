"""In-memory subprocess registry. Maps workspace_id → (AgentHandle,
last_touched_at, lock_session). Drives idle-timeout teardown."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    DraftingAgent,
    LaunchContext,
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

    Concurrency: get_or_launch / terminate / sweep_idle are async and
    serialize through a single asyncio.Lock — there's no win from
    parallelism here (the bottleneck is the SDK's launch, which is
    sub-second). The lock also prevents the launch-vs-sweep race where
    a workspace is launched while sweep_idle is iterating.
    """

    def __init__(self, *, adapter: DraftingAgent, idle_seconds: int) -> None:
        self._adapter = adapter
        self._idle_seconds = idle_seconds
        self._entries: dict[uuid.UUID, _Entry] = {}
        self._lock = asyncio.Lock()

    async def get_or_launch_if_exists(
        self,
        workspace_id: uuid.UUID,
    ) -> AgentHandle | None:
        """Return the cached handle if one exists (and refresh its touch
        timestamp); else None. Does NOT call adapter.launch."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                return None
            entry.last_touched_monotonic = time.monotonic()
            return entry.handle

    async def get_or_launch(
        self,
        workspace_id: uuid.UUID,
        *,
        context: LaunchContext | None = None,
        lock_session: Session | None = None,
    ) -> AgentHandle:
        async with self._lock:
            existing = self._entries.get(workspace_id)
            if existing is not None:
                existing.last_touched_monotonic = time.monotonic()
                return existing.handle

            if context is None:
                context = LaunchContext(workspace_id=workspace_id)
            handle = await self._adapter.launch(context)
            self._entries[workspace_id] = _Entry(
                handle=handle,
                last_touched_monotonic=time.monotonic(),
                lock_session=lock_session,
            )
            return handle

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
                from call_monitor.proposal_assist.drafting.locking import (
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
        # (subprocess teardown) and we don't want to block get_or_launch.
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
                        from call_monitor.proposal_assist.drafting.locking import (
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
        user_message: str,
    ) -> AsyncIterator[StreamEvent]:
        """Pass-through to the adapter's send_turn. The controller calls
        touch() after the iterator is fully consumed."""
        async for event in self._adapter.send_turn(handle, user_message):
            yield event
