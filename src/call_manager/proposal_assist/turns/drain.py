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

import logging

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.turns.processor import process_turn_once

logger = logging.getLogger(__name__)


async def drain_turn_queue_tick(
    *,
    session_factory: sessionmaker,
    controller: WorkspaceController,
) -> int:
    """Find workspaces with queued turn_jobs; pick the oldest queued
    job per workspace; dispatch process_turn_once for each. Returns the
    count of dispatched turns."""
    with session_factory() as s:
        # Exclude workspaces that already have a running turn — otherwise
        # the atomic claim in process_turn_once would trip the partial
        # unique index on turn_jobs(workspace_id) WHERE status='running',
        # raising IntegrityError once per drain tick under any moderately
        # busy workspace.
        running_subq = (
            select(TurnJob.workspace_id).where(TurnJob.status == "running")
        ).scalar_subquery()
        stmt = (
            select(TurnJob.id, TurnJob.workspace_id)
            .where(TurnJob.status == "queued")
            .where(TurnJob.workspace_id.notin_(running_subq))
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
            await process_turn_once(
                session_factory=session_factory,
                controller=controller,
                turn_job_id=turn_id,
            )
            dispatched += 1
        except Exception:
            logger.exception(
                "drain_turn_queue_tick: process_turn_once raised for turn_job %s "
                "(workspace %s) — continuing to next workspace",
                turn_id,
                workspace_id,
            )
    return dispatched
