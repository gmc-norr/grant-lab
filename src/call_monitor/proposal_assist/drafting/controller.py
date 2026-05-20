"""WorkspaceController composes the drafting machinery:

  - acquire per-workspace advisory lock (held for the handle's lifetime)
  - sweep orphaned turn_jobs rows (inside the lock)
  - build resume-from-history messages (inside the lock)
  - launch (or reuse) the subprocess via the registry
  - expose send_turn / terminate as a thin pass-through

This is the only object the web layer (B6) needs to construct."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from sqlalchemy.orm import sessionmaker

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    LaunchContext,
    StreamEvent,
)
from call_monitor.proposal_assist.drafting.locking import (
    LockNotAcquired,
    acquire_workspace_lock,
    release_workspace_lock,
)
from call_monitor.proposal_assist.drafting.orphan_sweep import (
    sweep_orphaned_turn_jobs,
)
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
from call_monitor.proposal_assist.drafting.resume import (
    build_resume_messages,
)


class WorkspaceController:
    def __init__(
        self,
        *,
        registry: SubprocessRegistry,
        session_factory: sessionmaker,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory

    async def launch(self, workspace_id: uuid.UUID) -> AgentHandle:
        """Launch (or return cached) handle, coalescing concurrent callers.

        Concurrent calls for the same workspace_id are serialized via
        the registry's pending-launch futures: only the first caller
        becomes the launcher; all subsequent concurrent callers await
        that caller's result.  This eliminates the spurious
        LockNotAcquired that previously occurred when two coroutines
        raced through get_or_launch_if_exists before any entry existed.

        Launch sequence (launcher path only):
        1. Opens a lock-holding session (raises LockNotAcquired if
           another process holds the lock).  The session is never
           committed — keeping the connection checked out is what
           maintains the Postgres session-level advisory lock.
        2. Runs the orphan sweep + builds resume messages in a SHORT-
           LIVED sibling session (committed and closed immediately)
           while we hold the lock, so no other process can race us.
        3. Calls adapter.launch() to start the subprocess.
        4. Hands the lock-holding session to the registry via
           fulfill_pending_launch() — the registry keeps it open (and
           therefore the lock held) for the handle's lifetime.

        Error handling: if any step raises, fail_pending_launch() is
        called so that waiting coroutines receive the exception rather
        than hanging forever.  lock_session is always released + closed
        before the exception propagates.

        Known limit: if fulfill_pending_launch itself raises (dict ops /
        future.set_result — neither should in practice), the handle
        leaks.  This is documented as an acceptable trade-off.
        """
        am_launcher, pending = await self._registry.claim_launch_or_wait(workspace_id)
        if not am_launcher:
            # Another coroutine is already launching (or entry is cached).
            # pending is a resolved or in-progress Future[AgentHandle].
            return await pending  # type: ignore[return-value]

        # We are the launcher.
        try:
            lock_session = self._session_factory()
            if not acquire_workspace_lock(lock_session, workspace_id):
                lock_session.close()
                raise LockNotAcquired(workspace_id)

            try:
                # Run sweep + resume in a separate committed session so the
                # orphan-sweep UPDATE is visible to other sessions, while
                # lock_session stays open (uncommitted) to hold the advisory
                # lock on its connection.
                with self._session_factory() as work_session:
                    sweep_orphaned_turn_jobs(work_session, workspace_id)
                    resume_messages = build_resume_messages(work_session, workspace_id)
                    work_session.commit()
                context = LaunchContext(
                    workspace_id=workspace_id,
                    resume_messages=resume_messages,
                )
                handle = await self._registry._adapter.launch(context)
            except Exception:
                release_workspace_lock(lock_session, workspace_id)
                lock_session.close()
                raise

        except BaseException as e:
            await self._registry.fail_pending_launch(workspace_id, e)
            raise

        await self._registry.fulfill_pending_launch(
            workspace_id,
            handle,
            lock_session=lock_session,
        )
        return handle

    async def send_turn(
        self,
        workspace_id: uuid.UUID,
        user_message: str,
    ) -> AsyncIterator[StreamEvent]:
        """Send a turn and stream events.

        in_flight is incremented before iteration begins and decremented in a
        finally block so that sweep_idle never terminates a handle mid-stream,
        even if the turn takes longer than workspace_idle_timeout_seconds.
        decrement_in_flight also bumps last_touched_monotonic, so a separate
        touch() call is no longer needed."""
        handle = await self.launch(workspace_id)
        await self._registry.increment_in_flight(workspace_id)
        try:
            async for event in self._registry.send_turn(handle, user_message):
                yield event
        finally:
            await self._registry.decrement_in_flight(workspace_id)

    async def terminate(self, workspace_id: uuid.UUID) -> None:
        await self._registry.terminate(workspace_id)
