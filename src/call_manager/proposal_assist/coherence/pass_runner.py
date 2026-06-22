"""Run one coherence pass for a claimed (running) coherence_jobs row, and the
async drain tick that claims+runs one job. The pass is fresh-stateless: a dedicated
one-shot adapter, no resume, terminated in finally; output is proposed_change rows."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, Workspace
from call_manager.proposal_assist.coherence.agent import build_keeper_adapter
from call_manager.proposal_assist.coherence.context import assemble_coherence_context
from call_manager.proposal_assist.coherence.jobs import (
    claim_next_coherence_job,
    coherence_cap_exceeded,
    supersede_own_coherence,
    supersede_prior_coherence,
)
from call_manager.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent

logger = logging.getLogger(__name__)


async def run_coherence_pass(
    *,
    job_id: uuid.UUID,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_keeper_adapter,
) -> None:
    """Run the pass for an already-claimed (running) job.

    The launch+drive portion is fully guarded (any error/timeout fails the job and
    supersedes its own partial rows). The initial short read session (job/workspace
    read, context assembly, cap check) runs before that guard, so a DB/file error
    there can propagate; the scheduler tick wraps this call and the reaper reclaims a
    job left 'running'.
    """
    # 1. Read job + workspace state (lock, requested_by) in a short session.
    with session_factory() as s:
        job = s.get(CoherenceJob, job_id)
        if job is None or job.status != "running":
            return
        ws = s.get(Workspace, job.workspace_id)
        workspace_id = job.workspace_id
        requested_by = job.requested_by
        locked = ws is not None and ws.spec_locked_at is not None
        if coherence_cap_exceeded(s, workspace_id=workspace_id, settings=settings):
            _fail(s, job, "daily coherence budget reached")
            s.commit()
            return
        instruction = assemble_coherence_context(
            session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
        )

    adapter = adapter_builder(
        settings=settings,
        session_factory=session_factory,
        workspace_id=workspace_id,
        requested_by=requested_by,
        job_id=job_id,
        locked=locked,
    )
    handle = None
    done: TurnDoneEvent | None = None
    try:

        async def _run() -> TurnDoneEvent | None:
            nonlocal handle
            # launch is INSIDE the wait_for so a hung SDK connect is bounded too — not
            # just the streaming turn. wait_for cancels this coroutine on timeout,
            # raising CancelledError at whichever await is live (launch or the loop).
            handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
            last = None
            async for event in adapter.send_turn(handle, instruction):
                if isinstance(event, TurnDoneEvent):
                    last = event
            return last

        done = await asyncio.wait_for(_run(), timeout=settings.coherence_pass_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — terminal: fail the job, never propagate
        logger.exception("coherence pass failed for job %s", job_id)
        # Member-visible: surface only a sanitized, generic message (full detail
        # already in the logs above); distinguish the expected timeout outcome.
        message = (
            "Coherence check timed out."
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            else "Coherence check failed."
        )
        with session_factory() as s:
            # Lock the row + guard on status=='running': the separate reaper may
            # have already failed this job during a hang; don't double-finalize.
            job = s.get(CoherenceJob, job_id, with_for_update=True)
            if job is not None and job.status == "running":
                _fail(s, job, message)
                supersede_own_coherence(s, workspace_id=workspace_id, job_id=job_id)
            s.commit()
        return
    finally:
        if handle is not None:
            try:
                # Bound cleanup too: a hung SDK disconnect must not leak the pass
                # coroutine (the scheduler has already stopped awaiting it). Kept well
                # under the scheduler's coherence_pass_timeout_seconds+60 future grace.
                await asyncio.wait_for(
                    adapter.terminate(handle),
                    timeout=settings.coherence_terminate_timeout_seconds,
                )
            except Exception:  # noqa: BLE001
                logger.exception("coherence adapter terminate failed for job %s", job_id)

    # 2. Success: claim the terminal transition under a row lock; ONLY finalize if
    #    the job is still 'running' (the reaper may have failed it during a hang —
    #    in which case its own cleanup already ran and we must not resurrect it).
    with session_factory() as s:
        from sqlalchemy import func, select

        from call_manager.db.models import ProposedChange

        job = s.get(CoherenceJob, job_id, with_for_update=True)
        if job is None or job.status != "running":
            s.commit()
            return
        supersede_prior_coherence(s, workspace_id=workspace_id, job_id=job_id)
        n = s.scalar(
            select(func.count())
            .select_from(ProposedChange)
            .where(
                ProposedChange.workspace_id == workspace_id,
                ProposedChange.origin_job_id == job_id,
                ProposedChange.status == "pending",
            )
        )
        job.status = "completed"
        job.completed_at = datetime.now(UTC)
        job.n_proposals = int(n or 0)
        if done is not None:
            job.input_tokens = done.tokens_in or 0
            job.output_tokens = done.tokens_out or 0
            job.model = done.model
            job.sdk_session_id = done.session_id
        s.commit()


def _fail(session, job: CoherenceJob, message: str) -> None:
    job.status = "failed"
    job.error = message
    job.completed_at = datetime.now(UTC)
    session.flush()


async def drain_coherence_queue_tick(
    *,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_keeper_adapter,
) -> None:
    """Claim one job and run it. The reaper is a separate sync tick (Task 7)."""
    with session_factory() as s:
        job = claim_next_coherence_job(s)
        s.commit()
        job_id = job.id if job is not None else None
    if job_id is None:
        return
    await run_coherence_pass(
        job_id=job_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
