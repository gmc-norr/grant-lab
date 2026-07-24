"""In-memory subprocess registry. Maps workspace_id → (AgentHandle,
last_touched_at, lock_session). Drives idle-timeout teardown."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from call_manager.proposal_assist.drafting.contract import (
    AgentHandle,
    DraftingAgent,
    DraftingAtCapacity,
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

    def __init__(
        self,
        *,
        adapter: DraftingAgent,
        idle_seconds: int,
        max_live: int | None = None,
    ) -> None:
        self._adapter = adapter
        self._idle_seconds = idle_seconds
        # Defect 1 (resource exhaustion): hard ceiling on concurrent live
        # subprocesses. Each live handle pins ONE pooled DB connection for its
        # whole life (the session-level advisory lock holds it checked out, up
        # to workspace_idle_timeout_seconds). The engine pool is pool_size=10 +
        # max_overflow=20 = 30 connections shared with the WHOLE app (every web
        # request, every scheduler tick). Left unbounded, ~30 active workspaces
        # would consume the entire pool and stall the app. max_live caps the
        # live set well below 30 (default DRAFTING_MAX_LIVE_SUBPROCESSES=8) so
        # drafting can never starve the request/scheduler pool. In production
        # those lock sessions also come from a DEDICATED engine sized to this
        # ceiling (see db/session.make_drafting_engine + factory.py), so even at
        # the cap no drafting connection is drawn from the request pool.
        # None = unbounded (legacy/test path with no ceiling).
        self._max_live = max_live
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

        Capacity (Defect 1): when the caller would be the launcher of a NET-NEW
        subprocess and the live set is at the ceiling, the LEAST-recently-touched
        IDLE handle (in_flight == 0) is evicted to make room. If every live
        handle is mid-turn (none evictable), the launch is refused with
        :class:`DraftingAtCapacity` so the API layer returns a retriable 429 and
        the drain tick leaves the queued turn for next time. Cached/already-
        launching workspaces bypass this entirely — only net-new launches count
        against the ceiling.

        Unconfirmed-dead generations (retiring records in the adapter's live-client
        inventory) also occupy slots, and the ceiling is RE-CHECKED after an
        eviction's teardown: if the victim's death is unconfirmed the replacement is
        refused with DraftingAtCapacity (retriable 429) instead of exceeding the cap.
        """
        victim: _Entry | None = None
        victim_id: uuid.UUID | None = None
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

            # Net-new launch. Enforce the live-subprocess ceiling. The "in use"
            # count is live entries + in-flight pending launches (each pending
            # launch will become a live entry holding a connection). Evict the
            # LRU idle handle if at the cap; refuse if none are idle.
            if self._max_live is not None:
                in_use = (
                    len(self._entries)
                    + len(self._pending_launches)
                    + self._unconfirmed_generations()
                )
                if in_use >= self._max_live:
                    victim_id = self._pick_lru_idle_victim_locked()
                    if victim_id is None:
                        # Every live handle is mid-turn — can't free a slot.
                        raise DraftingAtCapacity(
                            f"drafting at capacity ({self._max_live} live subprocesses, "
                            "all mid-turn); retry shortly"
                        )
                    victim = self._entries.pop(victim_id)

            # We are the launcher — install a new future and return.
            new_future: asyncio.Future[AgentHandle] = asyncio.get_running_loop().create_future()
            self._pending_launches[workspace_id] = new_future

        # Terminate the evicted victim OUTSIDE the lock (adapter.terminate may be
        # slow) so we don't block other claim/sweep callers. The pending-launch
        # slot is already installed, so concurrent same-workspace callers coalesce
        # correctly while this runs.
        if victim is not None and victim_id is not None:
            await self._teardown_entry(victim_id, victim, context="capacity-evict")
            # The teardown may have ended UNCONFIRMED, in which case the victim's
            # process (and its pooled connection) may still be alive and the slot
            # was never really freed. Re-check before launching the replacement;
            # surrender the reservation rather than exceed the ceiling.
            async with self._lock:
                over = self._max_live is not None and (
                    len(self._entries)
                    + len(self._pending_launches)
                    + self._unconfirmed_generations()
                ) > self._max_live
            if over:
                exc = DraftingAtCapacity(
                    f"drafting at capacity ({self._max_live} live subprocesses; an "
                    "evicted subprocess is not confirmed dead); retry shortly"
                )
                await self.fail_pending_launch(workspace_id, exc)
                raise exc
        return (True, None)

    async def reconcile_deleted_entries(self, is_deleted) -> int:
        """Pop and tear down entries whose workspace is soft-deleted, REGARDLESS of
        in_flight (a running turn on a deleted workspace is exactly the target; the
        idle sweep deliberately skips those). _teardown_entry also releases the
        advisory lock and closes the lock_session — something adapter-level
        reconciliation cannot do. Returns the number torn down."""
        async with self._lock:
            candidates = list(self._entries.keys())
        # Blocking DB predicate OUTSIDE the lock (I2), then re-acquire to pop.
        deleted = [wsid for wsid in candidates if is_deleted(wsid)]
        doomed: list[tuple[uuid.UUID, _Entry]] = []
        async with self._lock:
            for wsid in deleted:
                entry = self._entries.pop(wsid, None)
                if entry is not None:
                    doomed.append((wsid, entry))
        for wsid, entry in doomed:
            await self._teardown_entry(wsid, entry, context="retire-reconcile")
        return len(doomed)

    def _unconfirmed_generations(self) -> int:
        """Generations whose death the adapter has not confirmed. They may still
        pin a subprocess and a pooled DB connection, so they occupy slots.
        Adapters without an inventory report 0."""
        counter = getattr(self._adapter, "retiring_count", None)
        return counter() if counter is not None else 0

    def _pick_lru_idle_victim_locked(self) -> uuid.UUID | None:
        """Return the workspace_id of the least-recently-touched IDLE handle
        (in_flight == 0), or None if every live handle is mid-turn. Caller must
        hold self._lock."""
        idle = [(wsid, entry) for wsid, entry in self._entries.items() if entry.in_flight == 0]
        if not idle:
            return None
        return min(idle, key=lambda kv: kv[1].last_touched_monotonic)[0]

    async def _teardown_entry(
        self,
        workspace_id: uuid.UUID,
        entry: _Entry,
        *,
        context: str,
    ) -> None:
        """Terminate an already-popped entry's subprocess then release its
        advisory lock + close its lock_session. Shared by capacity eviction and
        sweep_idle so the teardown ordering (adapter first, then lock) is
        identical everywhere. Never raises — failures are logged."""
        try:
            await self._adapter.terminate(entry.handle)
        except Exception:
            logger.exception("%s: adapter.terminate raised for workspace %s", context, workspace_id)
        finally:
            if entry.lock_session is not None:
                try:
                    from call_manager.proposal_assist.drafting.locking import (
                        release_workspace_lock,
                    )

                    release_workspace_lock(entry.lock_session, workspace_id)
                    entry.lock_session.close()
                except Exception:
                    logger.exception(
                        "%s: failed to release lock for workspace %s",
                        context,
                        workspace_id,
                    )

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

    async def increment_in_flight(self, workspace_id: uuid.UUID) -> None:
        """TEST-ONLY in-flight increment (no production caller).

        Production turns begin via ``begin_in_flight_exclusive`` (which is
        atomic test-and-increment); this non-exclusive variant only exists so
        registry tests can simulate an active handle when exercising sweep_idle.
        Do not use it on the turn path — it would allow two concurrent turns
        against one subprocess."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is not None:
                entry.in_flight += 1
                entry.last_touched_monotonic = time.monotonic()

    async def begin_in_flight_exclusive(self, workspace_id: uuid.UUID) -> bool:
        """Atomically begin a turn IFF no other turn is in flight for this
        workspace. Returns True on success (in_flight incremented), False if a
        turn is already live (caller must NOT start) or the handle is gone.

        This serializes turns at the subprocess level: the DB partial-unique
        index normally guarantees one running turn_jobs row per workspace, but
        the stuck-turn reaper can free that row while the original turn's
        coroutine is still streaming. Without this guard the next drain tick
        would start a second send_turn against the SAME ClaudeSDKClient,
        interleaving transcripts and racing proposal edits. Checked under the
        same lock that guards in_flight so the test-and-increment is atomic."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None or entry.in_flight > 0:
                return False
            entry.in_flight += 1
            entry.last_touched_monotonic = time.monotonic()
            return True

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

    async def sweep_idle(
        self,
        *,
        has_queued_turn: Callable[[uuid.UUID], bool] | None = None,
    ) -> int:
        """Terminate any handle untouched for >= idle_seconds. Returns
        the count of swept handles. Intended for periodic scheduler
        invocation.

        Defect 4: a workspace that still has a queued turn is SKIPPED — tearing
        its resumable session down would only force a cold relaunch on the next
        drain tick. The scheduler passes a ``has_queued_turn`` predicate (a DB
        lookup) so the sweep is queue-aware; when omitted (tests/legacy) every
        idle handle is swept as before.

        I2 fix: the ``has_queued_turn`` predicate may run a BLOCKING DB query, so
        it must NOT be consulted while the registry lock is held — that would
        stall every concurrent launch/turn. We therefore (1) snapshot the
        candidate idle workspace ids under the lock, (2) RELEASE the lock and run
        the predicate per candidate, then (3) re-acquire the lock and pop only
        the still-idle, non-queued entries (re-checking ``last_touched`` /
        ``in_flight`` so a turn that landed during the predicate window is not
        torn down out from under itself)."""
        now = time.monotonic()
        cutoff = now - self._idle_seconds

        # (1) Snapshot candidate idle workspace ids under the lock — no IO here.
        async with self._lock:
            candidates: list[uuid.UUID] = [
                wsid
                for wsid, entry in self._entries.items()
                if entry.last_touched_monotonic <= cutoff and entry.in_flight == 0
            ]

        # (2) Run the (possibly blocking) queued-turn predicate WITHOUT the lock.
        queued: set[uuid.UUID] = set()
        if has_queued_turn is not None:
            for wsid in candidates:
                if has_queued_turn(wsid):
                    queued.add(wsid)

        # (3) Re-acquire and pop only the entries that are STILL idle and not
        # queued. Re-check last_touched/in_flight: a turn may have landed during
        # the predicate window, in which case the entry must not be swept.
        cutoff = time.monotonic() - self._idle_seconds
        stale_entries: list[tuple[uuid.UUID, _Entry]] = []
        async with self._lock:
            for wsid in candidates:
                if wsid in queued:
                    # A queued turn will relaunch immediately — keep the warm,
                    # resumable handle instead of tearing it down.
                    continue
                entry = self._entries.get(wsid)
                if entry is None:
                    continue
                if entry.last_touched_monotonic > cutoff or entry.in_flight != 0:
                    # Re-touched / went in-flight while we were off the lock.
                    continue
                stale_entries.append((wsid, self._entries.pop(wsid)))

        # Terminate outside the lock — adapter.terminate may take a while
        # (subprocess teardown) and we don't want to block claim_launch_or_wait.
        # Order (in _teardown_entry): terminate the adapter FIRST, then release
        # the advisory lock — so another caller can't acquire the lock while the
        # old subprocess is still disconnecting (same ordering as terminate()).
        for wsid, entry in stale_entries:
            await self._teardown_entry(wsid, entry, context="sweep_idle")
        return len(stale_entries)

    async def interrupt(self, workspace_id: uuid.UUID) -> bool:
        """Best-effort graceful interrupt of the workspace's in-flight turn
        (Defect 5). Returns True if an interrupt was attempted, False if there
        was no live handle or the adapter doesn't support interrupt. Never tears
        the handle down — the caller still terminates as the guaranteed backstop.

        The adapter's interrupt is OPTIONAL (the Fake doesn't implement it), so
        we probe via getattr and skip when absent rather than requiring every
        adapter to provide a no-op."""
        async with self._lock:
            entry = self._entries.get(workspace_id)
            handle = entry.handle if entry is not None else None
        if handle is None:
            return False
        interrupt = getattr(self._adapter, "interrupt", None)
        if interrupt is None:
            return False
        await interrupt(handle)
        return True

    async def send_turn(
        self,
        handle: AgentHandle,
        user_message: str | list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        """Pass-through to the adapter's send_turn. The controller calls
        touch() after the iterator is fully consumed."""
        async for event in self._adapter.send_turn(handle, user_message):
            yield event

    async def set_turn_provenance(
        self,
        workspace_id: uuid.UUID,
        *,
        triggering_turn_id: uuid.UUID | None,
        triggering_prompt: str | None,
        created_by: int | None,
        origin_session_id: str | None,
        allowed_tool_names: set[str] | None = None,
        turn_selection: dict | None = None,
    ) -> None:
        """Pass-through to the adapter so the processor can stamp provenance on
        the cached handle's ToolContext before each turn."""
        await self._adapter.set_turn_provenance(
            workspace_id,
            triggering_turn_id=triggering_turn_id,
            triggering_prompt=triggering_prompt,
            created_by=created_by,
            origin_session_id=origin_session_id,
            allowed_tool_names=allowed_tool_names,
            turn_selection=turn_selection,
        )
