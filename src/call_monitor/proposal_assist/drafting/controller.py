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
        """Launch (or return cached) handle. Each fresh launch:
        1. Opens a lock-holding session (raises LockNotAcquired if
           another process holds the lock). The session is never
           committed — keeping the connection checked out is what
           maintains the Postgres session-level advisory lock.
        2. Runs the orphan sweep + builds resume messages in a SHORT-
           LIVED sibling session (committed and closed immediately)
           that executes while we hold the lock, so no other process
           can race us.
        3. Builds a LaunchContext and asks the registry to launch.
        4. Hands the lock-holding session to the registry — the
           registry keeps it open (and therefore the lock held) for
           the handle's lifetime.
        """
        cached = await self._registry.get_or_launch_if_exists(workspace_id)
        if cached is not None:
            return cached

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
        except Exception:
            release_workspace_lock(lock_session, workspace_id)
            lock_session.close()
            raise

        context = LaunchContext(
            workspace_id=workspace_id,
            resume_messages=resume_messages,
        )
        handle = await self._registry.get_or_launch(
            workspace_id,
            context=context,
            lock_session=lock_session,
        )
        return handle

    async def send_turn(
        self,
        workspace_id: uuid.UUID,
        user_message: str,
    ) -> AsyncIterator[StreamEvent]:
        """Send a turn and stream events. Touches the registry's idle
        timer on completion so the handle doesn't get swept mid-flight."""
        handle = await self.launch(workspace_id)
        async for event in self._registry.send_turn(handle, user_message):
            yield event
        await self._registry.touch(workspace_id)

    async def terminate(self, workspace_id: uuid.UUID) -> None:
        await self._registry.terminate(workspace_id)
