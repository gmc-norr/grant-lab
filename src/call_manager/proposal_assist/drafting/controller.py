"""WorkspaceController composes the drafting machinery:

  - acquire per-workspace advisory lock (held for the handle's lifetime)
  - sweep orphaned turn_jobs rows (inside the lock)
  - launch (or reuse) the subprocess via the registry
  - expose send_turn / terminate as a thin pass-through

This is the only object the web layer (B6) needs to construct."""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.contract import (
    AgentHandle,
    LaunchContext,
    SDKTurnError,
    StreamEvent,
    WorkspaceRetired,
)
from call_manager.proposal_assist.drafting.locking import (
    LockNotAcquired,
    acquire_workspace_lock,
    release_workspace_lock,
)
from call_manager.proposal_assist.drafting.orphan_sweep import (
    sweep_orphaned_turn_jobs,
)
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from call_manager.proposal_assist.reconcile import (
    reconcile_workspace_citekeys,
    reconcile_workspace_git_drift,
)

logger = logging.getLogger(__name__)


class WorkspaceController:
    def __init__(
        self,
        *,
        registry: SubprocessRegistry,
        session_factory: sessionmaker,
        workspaces_dir: Path | str = "var/workspaces",
        lock_session_factory: sessionmaker | None = None,
    ) -> None:
        self._registry = registry
        self._session_factory = session_factory
        # (see `registry` property below — the retirement wiring in main.py needs
        # the registry, which the factory does not otherwise expose)
        # Defect 1: the long-held advisory-lock session (kept checked out for
        # the subprocess's whole life) is opened from lock_session_factory — a
        # DEDICATED small pool sized to the live-subprocess ceiling — so it never
        # draws on the shared request/scheduler pool. Short-lived work (orphan
        # sweep, reconciliation, queued-turn lookup) stays on session_factory.
        # Defaults to session_factory when no dedicated factory is wired (tests),
        # preserving the original single-pool behavior.
        self._lock_session_factory = lock_session_factory or session_factory
        # Production callers pass settings.workspaces_dir explicitly;
        # the default keeps existing tests that omit this parameter working.
        self._workspaces_dir = Path(workspaces_dir)

    @property
    def registry(self) -> SubprocessRegistry:
        """The subprocess registry this controller drives.

        Public because build_workspace_controller returns only the controller, so
        the retirement wiring in main.py has no other way to reach
        registry.retire_workspace when it registers the "turns" hook."""
        return self._registry

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
        2. Runs the orphan sweep in a SHORT-LIVED sibling session
           (committed and closed immediately) while we hold the lock,
           so no other process can race us.
        3. Calls adapter.launch() to start the subprocess.
        4. Hands the lock-holding session to the registry via
           fulfill_pending_launch() — the registry keeps it open (and
           therefore the lock held) for the handle's lifetime.

        Error handling: if any step raises (including CancelledError),
        fail_pending_launch() is called so that waiting coroutines
        receive the exception rather than hanging forever. All partially
        acquired resources are cleaned up uniformly in the outer except:
        handle is terminated (if launched), advisory lock is released
        (if acquired), lock_session is closed (if opened). Cleanup uses
        contextlib.suppress so a secondary failure doesn't mask the
        original exception.

        Known limit: if fulfill_pending_launch itself raises (dict ops /
        future.set_result — neither should in practice), the handle
        leaks.  This is documented as an acceptable trade-off.
        """
        # Durable no-relaunch floor: refuse a soft-deleted workspace BEFORE
        # claim_launch_or_wait, because the cached/pending path returns early below
        # and never reaches the work_session block. Restart-safe; needs no wiring.
        # NARROWING ONLY — a deletion committing after this check can still see a
        # cached handle returned; reconciliation is the backstop.
        if self._workspace_is_deleted(workspace_id):
            raise WorkspaceRetired(workspace_id)

        am_launcher, pending = await self._registry.claim_launch_or_wait(workspace_id)
        if not am_launcher:
            # Another coroutine is already launching (or entry is cached).
            # pending is a resolved or in-progress Future[AgentHandle].
            return await pending  # type: ignore[return-value]

        # We are the launcher. Track ownership of partially-acquired
        # resources so the outer except can clean up uniformly across
        # every failure mode (factory raise, acquire raise, sweep raise,
        # adapter.launch raise, CancelledError between launch and fulfill,
        # fulfill itself raising). The order on cleanup is terminate handle
        # → release lock → close session, mirroring the registry's normal
        # teardown. `handed_off` tracks whether ownership has transferred
        # to the registry — once True, cleanup must NOT run.
        lock_session: Session | None = None
        lock_acquired = False
        handle: AgentHandle | None = None
        handed_off = False

        try:
            # Long-held lock session from the DEDICATED drafting pool (Defect 1).
            lock_session = self._lock_session_factory()
            if not acquire_workspace_lock(lock_session, workspace_id):
                raise LockNotAcquired(workspace_id)
            lock_acquired = True

            # Run sweep in a separate committed session so the orphan-sweep
            # UPDATE is visible to other sessions, while lock_session stays
            # open (uncommitted) to hold the advisory lock on its connection.
            # B4 will also fetch the workspace's last_sdk_session_id here to
            # populate LaunchContext.resume_session_id for native resume.
            with self._session_factory() as work_session:
                sweep_orphaned_turn_jobs(work_session, workspace_id)
                ws_row = work_session.scalar(select(Workspace).where(Workspace.id == workspace_id))
                resume_session_id = ws_row.last_sdk_session_id if ws_row is not None else None
                work_session.commit()
            # B5: backfill orphan git commits (artifact_versions rows missing
            # for committed proposal.md changes). Idempotent. Failure logged
            # but doesn't block launch — the subprocess can still run;
            # reconciliation will retry on the next workspace open.
            try:
                repo_dir = self._workspaces_dir / str(workspace_id) / "repo"
                reconcile_workspace_git_drift(
                    self._session_factory,
                    workspace_id,
                    repo_dir,
                )
                # Also repair the references.citekey cache from the canonical
                # ledger (a prior best-effort cache write may have failed). Same
                # contract: forward-only, idempotent, never blocks launch.
                reconcile_workspace_citekeys(
                    self._session_factory,
                    workspace_id,
                    repo_dir,
                )
            except Exception:
                logger.exception(
                    "controller.launch: reconciliation failed for %s; continuing",
                    workspace_id,
                )

            context = LaunchContext(
                workspace_id=workspace_id,
                resume_session_id=resume_session_id,
            )
            handle = await self._registry._adapter.launch(context)
            installed = await self._registry.fulfill_pending_launch(
                workspace_id,
                handle,
                lock_session=lock_session,
            )
            if not installed:
                # Retired while this launch was in flight (B-retire): nothing was
                # installed, so we still own the fresh handle + lock + session.
                # Raise BEFORE handed_off=True so the except below performs the
                # normal teardown — adapter-level termination alone would leak the
                # advisory lock and its session.
                raise WorkspaceRetired(workspace_id)
            handed_off = True

        except BaseException as e:
            if not handed_off:
                if handle is not None:
                    with contextlib.suppress(Exception):
                        await self._registry._adapter.terminate(handle)
                if lock_acquired and lock_session is not None:
                    with contextlib.suppress(Exception):
                        release_workspace_lock(lock_session, workspace_id)
                if lock_session is not None:
                    with contextlib.suppress(Exception):
                        lock_session.close()
                await self._registry.fail_pending_launch(workspace_id, e)
            raise

        return handle

    async def send_turn(
        self,
        workspace_id: uuid.UUID,
        user_message: str | list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        """Send a turn and stream events.

        in_flight is incremented before iteration begins and decremented in a
        finally block so that sweep_idle never terminates a handle mid-stream,
        even if the turn takes longer than workspace_idle_timeout_seconds.
        decrement_in_flight also bumps last_touched_monotonic, so the handle's
        idle clock resets when the turn ends without a separate call.

        Concurrency guard: begin_in_flight_exclusive refuses to start if another
        turn is already streaming for this workspace (e.g. the reaper freed the
        DB row while the prior turn's coroutine is still live). That would run
        two turns against one SDK subprocess; instead we raise SDKTurnError so
        the second turn finalizes as failed and the drain retries it once the
        first releases."""
        handle = await self.launch(workspace_id)
        if not await self._registry.begin_in_flight_exclusive(workspace_id):
            raise SDKTurnError(
                f"workspace {workspace_id} already has a turn in flight; "
                "refusing to run a second turn against the same subprocess"
            )
        try:
            async for event in self._registry.send_turn(handle, user_message):
                yield event
        finally:
            await self._registry.decrement_in_flight(workspace_id)

    async def terminate(self, workspace_id: uuid.UUID) -> None:
        await self._registry.terminate(workspace_id)

    async def interrupt(self, workspace_id: uuid.UUID) -> bool:
        """Best-effort graceful interrupt of the workspace's in-flight turn
        (Defect 5). Lets a timed-out turn stop cleanly so the subprocess can be
        reused, instead of always cold-terminating. Returns True if an interrupt
        was attempted. The caller MUST still terminate as the guaranteed
        backstop — interrupt is advisory."""
        return await self._registry.interrupt(workspace_id)

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
        """Stamp server-trusted provenance onto the workspace's retained
        ToolContext before send_turn. Called by process_turn_once every turn."""
        await self._registry.set_turn_provenance(
            workspace_id,
            triggering_turn_id=triggering_turn_id,
            triggering_prompt=triggering_prompt,
            created_by=created_by,
            origin_session_id=origin_session_id,
            allowed_tool_names=allowed_tool_names,
            turn_selection=turn_selection,
        )

    async def sweep_idle(self) -> int:
        """Terminate any handle untouched for >= the registry's idle window.

        Returns the number of handles swept. Intended for the scheduler
        tick — exposed here so the scheduler closure doesn't have to reach
        into _registry.

        Defect 4: workspaces with a queued turn are skipped so their resumable
        session isn't torn down only to cold-relaunch on the next drain tick.
        The predicate is a single indexed SELECT against turn_jobs in a short-
        lived session, opened per-call (never threaded through the registry).
        I2: the registry runs this predicate OUTSIDE its asyncio lock (snapshot
        candidates → run predicate → re-acquire to pop), so the blocking SELECT
        never stalls concurrent launches."""
        return await self._registry.sweep_idle(has_queued_turn=self._workspace_has_queued_turn)

    async def reconcile_live_clients(self) -> int:
        """Retry teardown for everything a deletion or a failed teardown left behind.

        TWO passes, in order: (1) the REGISTRY pass pops entries for soft-deleted
        workspaces even mid-turn and releases their advisory locks; (2) the ADAPTER
        pass retries detached generations — retiring records (whose workspace may be
        LIVE: capacity-eviction / idle-sweep / launcher-cleanup orphans) plus any
        generation of a deleted workspace. Guarantees a remediation ATTEMPT each
        tick, not that a process died. Never raises: one bad record must not kill
        the tick."""
        total = 0
        try:
            total += await self._registry.reconcile_deleted_entries(self._workspace_is_deleted)
        except Exception:
            logger.exception("reconcile_live_clients: registry pass failed")
        reconcile = getattr(self._registry._adapter, "reconcile_live_clients", None)
        if reconcile is not None:
            try:
                total += await reconcile(is_deleted=self._workspace_is_deleted)
            except Exception:
                logger.exception("reconcile_live_clients: adapter pass failed")
        return total

    def _workspace_is_deleted(self, workspace_id: uuid.UUID) -> bool:
        """True if the workspace row is missing or soft-deleted. Fresh short-lived
        session. Best-effort: on a DB error return False so a transient blip cannot
        block legitimate drafting — reconciliation still reaps a genuinely deleted
        workspace's subprocess."""
        try:
            with self._session_factory() as s:
                row = s.execute(
                    select(Workspace.soft_deleted_at).where(Workspace.id == workspace_id)
                ).first()
                return row is None or row[0] is not None
        except Exception:
            logger.exception(
                "launch: soft-delete lookup failed for %s; allowing launch", workspace_id
            )
            return False

    def _workspace_has_queued_turn(self, workspace_id: uuid.UUID) -> bool:
        """True if the workspace has at least one queued turn_jobs row. Opens a
        fresh short-lived session (the per-unit-of-work invariant in
        db/session.py). Best-effort: on any DB error, return False so the sweep
        still tears down a genuinely idle handle (the conservative default —
        never pin a connection on a lookup failure)."""
        from call_manager.db.models import TurnJob

        try:
            with self._session_factory() as s:
                return (
                    s.scalar(
                        select(TurnJob.id)
                        .where(TurnJob.workspace_id == workspace_id)
                        .where(TurnJob.status == "queued")
                        .limit(1)
                    )
                    is not None
                )
        except Exception:
            logger.exception(
                "sweep_idle: queued-turn lookup failed for %s; treating as no queue",
                workspace_id,
            )
            return False
