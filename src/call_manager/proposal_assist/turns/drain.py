"""Drain scheduler tick: pick one queued turn per workspace and dispatch.

Production trigger: APScheduler interval job at TURN_DRAIN_TICK_SECONDS
cadence. The closure passes the workspace_controller (constructed by
main.py's lifespan) into this function via run_coroutine_threadsafe
onto the app loop, matching the H1 pattern from the idle-sweep tick.

Tie-break: when two queued rows for one workspace share min(queued_at),
DISTINCT ON (workspace_id) with ORDER BY (workspace_id, queued_at, id)
deterministically picks the row with the lower id — the next tick picks
the other one.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import TurnJob, Workspace
from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.turns.processor import process_turn_once

logger = logging.getLogger(__name__)


async def drain_turn_queue_tick(
    *,
    session_factory: sessionmaker,
    controller: WorkspaceController,
    workspaces_dir: Path | None = None,
    drafting_enabled: bool = True,
    turn_max_seconds: float | None = None,
) -> int:
    """Find workspaces with queued turn_jobs; pick the oldest queued
    job per workspace; dispatch process_turn_once for each. Returns the
    count of dispatched turns.

    workspaces_dir is forwarded to process_turn_once for attachment
    resolution. Passing None uses the default 'var/workspaces' path,
    which is safe for pure-text turns but will raise for image blocks.

    drafting_enabled: when False, the tick is a no-op — queued turns
    wait for re-enable rather than being marked failed. The scheduler
    passes settings.drafting_enabled so an operator can flip the kill
    switch and restart without losing the queue.
    """
    if not drafting_enabled:
        logger.info("drain_turn_queue_tick: skipped (drafting_enabled=False)")
        return 0

    with session_factory() as s:
        # Exclude workspaces that already have a running turn — otherwise
        # the atomic claim in process_turn_once would trip the partial
        # unique index on turn_jobs(workspace_id) WHERE status='running',
        # raising IntegrityError once per drain tick under any moderately
        # busy workspace.
        running_subq = (
            select(TurnJob.workspace_id).where(TurnJob.status == "running")
        ).scalar_subquery()
        # Exclude soft-deleted workspaces: a deleted workspace must not keep
        # spending tokens / editing its repo on queued turns. (The soft_delete
        # handler also marks queued jobs failed, but this is the durable gate —
        # a turn queued in a race with deletion still won't be dispatched.)
        deleted_subq = (
            select(Workspace.id).where(Workspace.soft_deleted_at.is_not(None))
        ).scalar_subquery()
        stmt = (
            select(TurnJob.id, TurnJob.workspace_id)
            .where(TurnJob.status == "queued")
            .where(TurnJob.workspace_id.notin_(running_subq))
            .where(TurnJob.workspace_id.notin_(deleted_subq))
            .distinct(TurnJob.workspace_id)
            .order_by(TurnJob.workspace_id, TurnJob.queued_at, TurnJob.id)
        )
        rows = list(s.execute(stmt).all())

    if not rows:
        return 0

    # Sequential within tick: don't overwhelm the SDK's concurrency
    # budget. B3's per-workspace advisory lock + the registry's
    # pending-launch coalescing handle any contention from other paths
    # that might also call launch concurrently.
    dispatched = 0
    for turn_id, workspace_id in rows:
        try:
            coro = process_turn_once(
                session_factory=session_factory,
                controller=controller,
                turn_job_id=turn_id,
                workspaces_dir=workspaces_dir,
            )
            # Bound each turn in-loop. asyncio.wait_for cancels the coroutine
            # on timeout, which raises CancelledError at the streaming await
            # inside process_turn_once; that path finalizes the turn as failed
            # and releases the workspace's running-row CLEANLY. This is the
            # primary mechanism that keeps a slow/hung turn from being
            # abandoned by the scheduler's fut.result timeout and later reaped
            # while still live (the data-corruption window). The reaper remains
            # only as a backstop for a truly wedged loop.
            if turn_max_seconds is not None:
                await asyncio.wait_for(coro, timeout=turn_max_seconds)
            else:
                await coro
            dispatched += 1
        except TimeoutError:
            logger.warning(
                "drain_turn_queue_tick: turn_job %s (workspace %s) exceeded "
                "turn_max_seconds=%s and was cancelled+failed in-loop",
                turn_id,
                workspace_id,
                turn_max_seconds,
                extra={"event": "turn_timed_out", "turn_job_id": str(turn_id)},
            )
            # Cancelling the turn coroutine interrupts the streaming read but does
            # NOT drain the SDK subprocess: the abandoned turn's remaining messages
            # (incl. its ResultMessage) stay queued in the persistent client's
            # shared stream. If the cached handle is reused, the NEXT turn reads
            # those leftovers first — mis-attributing transcript rows + token
            # counts and desyncing every subsequent turn.
            #
            # Defect 5: first try a SHORT-bounded graceful interrupt() so the SDK
            # subprocess stops the current turn cleanly (which lets terminate's
            # disconnect flush + exit normally rather than via SIGTERM/SIGKILL).
            # interrupt is best-effort and advisory — terminate() below remains
            # the GUARANTEED backstop that frees the handle + advisory lock. Bound
            # the interrupt so a wedged subprocess can't stall the drain tick.
            try:
                await asyncio.wait_for(controller.interrupt(workspace_id), timeout=2.0)
            except Exception:
                # TimeoutError or any interrupt failure — fall straight through to
                # the terminate backstop. Not even logged at exception level: this
                # is an expected best-effort path.
                logger.debug(
                    "drain_turn_queue_tick: graceful interrupt for workspace %s "
                    "failed/timed out; falling back to terminate",
                    workspace_id,
                )
            # Terminate the handle so the next turn cold-launches a fresh, clean
            # subprocess (terminate is idempotent + releases the advisory lock;
            # guarded so a terminate failure can't abort the drain tick).
            try:
                await controller.terminate(workspace_id)
            except Exception:
                logger.exception(
                    "drain_turn_queue_tick: terminate after timeout failed for "
                    "workspace %s (handle may be stale until the reaper/idle sweep)",
                    workspace_id,
                )
        except Exception:
            logger.exception(
                "drain_turn_queue_tick: process_turn_once raised for turn_job %s "
                "(workspace %s) — continuing to next workspace",
                turn_id,
                workspace_id,
            )
    return dispatched
