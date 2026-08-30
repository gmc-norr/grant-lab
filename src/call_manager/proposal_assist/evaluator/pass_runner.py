"""Run one evaluator pass for a claimed (running) evaluator_jobs row, and the async
drain tick. Fresh-stateless: a dedicated one-shot adapter, no resume, terminated in
finally; output is the scorecard JSONB written by submit_scorecard. Carries the
SP2b-2c hardening: launch+drive bounded together, bounded terminate, FOR UPDATE
finalize guard. The separate sync reaper handles crash recovery."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent
from call_manager.proposal_assist.evaluator.agent import build_evaluator_adapter
from call_manager.proposal_assist.evaluator.context import assemble_evaluator_context
from call_manager.proposal_assist.evaluator.jobs import (
    claim_next_evaluator_job,
    evaluator_cap_exceeded,
)
from call_manager.proposal_assist.passes import PASS_REGISTRY
from call_manager.proposal_assist.row_locks import bounded_prefix_lock_waits
from call_manager.proposal_assist.spend import record_spend

logger = logging.getLogger(__name__)


def _fail(
    session, job: EvaluatorJob, message: str, done=None, status: str = "failed"
) -> None:
    job.status = "failed"
    job.error = message
    job.completed_at = datetime.now(UTC)
    # Keep the display cache honest on the failure path too. The only column
    # writers in each family sit on the SUCCESS path, and these columns are NOT
    # NULL with server_default "0" — so without this the ledger says 21 and the
    # UI says 0 for every failed run, which is the drift Task 9 exists to
    # prevent.
    job.input_tokens = (done.tokens_in or 0) if done is not None else 0
    job.output_tokens = (done.tokens_out or 0) if done is not None else 0
    # Spend happened even though the run did not finish. Recording it is the
    # whole point: the old code left every failed run invisible to the cap.
    record_spend(
        session,
        workspace_id=job.workspace_id,
        kind="evaluator",
        status=status,
        tokens_in=done.tokens_in if done else None,
        tokens_out=done.tokens_out if done else None,
        model=done.model if done else None,
        ref_id=job.id,
        sdk_session_id=done.session_id if done else None,
    )
    session.flush()


async def run_evaluator_pass(
    *,
    job_id: uuid.UUID,
    claimed_generation: int,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_evaluator_adapter,
) -> None:
    """Run the pass for an already-claimed (running) job.

    The launch+drive portion is fully guarded (any error/timeout fails the job —
    no supersede: the scorecard is one JSONB on the job, not separate rows). The
    initial short read session (job/workspace read, staleness-marker capture,
    context assembly, cap check) runs before that guard; the scheduler tick wraps
    this call and the separate reaper reclaims a job left 'running'.
    """
    # 1. Short read session: capture staleness markers + assemble context (pinned HEAD).
    # Tracked from the FIRST line, BEFORE the prefix: an entry registered
    # only after the prefix is invisible to a DELETE that commits during it.
    with session_factory() as s0:
        job0 = s0.get(EvaluatorJob, job_id)
        if job0 is None or job0.status != "running":
            return
        tracked_ws = job0.workspace_id

    async with PASS_REGISTRY.track(tracked_ws) as _entry:
        # bounded_prefix_lock_waits wraps the WHOLE prefix transaction, commit
        # included: the workspace row is held from the acquisition below until COMMIT,
        # across context assembly's filesystem reads and the job-row UPDATE. Without
        # the bound a stalled prefix pins the row against the owner's DELETE for as
        # long as the stall lasts — and that DELETE, past the commit mutex, now gives
        # up on the row after 2s and answers 429 having deleted nothing rather than
        # hanging (Slice C3). Bounding the prefix is what keeps the owner's DELETE
        # from failing at all. See proposal_assist/row_locks.py.
        with session_factory() as s, bounded_prefix_lock_waits(s):
            job = s.get(EvaluatorJob, job_id)
            if job is None or job.status != "running":
                return
            # Workspace ROW LOCK held through this session's commit — linearizes the
            # prefix against a concurrent delete. This prefix commits DURABLE progress
            # (evaluated_draft_sha / _spec_sha / _rubric_updated_at), so without the
            # guard those are writes against a deleted workspace.
            ws = s.get(Workspace, job.workspace_id, with_for_update=True)
            if ws is None or ws.soft_deleted_at is not None:
                return
            workspace_id = job.workspace_id
            requested_by = job.requested_by
            if evaluator_cap_exceeded(s, workspace_id=workspace_id, settings=settings):
                _fail(s, job, "daily evaluation budget reached")
                s.commit()
                return
            ctx = assemble_evaluator_context(
                session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
            )
            # The context hashed the EXACT bytes it assembled (single read) — use those so
            # the staleness markers match what the model actually evaluated.
            job.evaluated_draft_sha = ctx.draft_sha
            job.evaluated_spec_sha = ctx.spec_sha
            job.evaluated_rubric_updated_at = ws.rubric_updated_at if ws else None
            instruction = ctx.instruction
            draft_n, spec_n = ctx.draft_section_count, ctx.spec_section_count
            rubric_snapshot = ctx.rubric
            s.commit()

        adapter = adapter_builder(
            settings=settings,
            session_factory=session_factory,
            workspace_id=workspace_id,
            requested_by=requested_by,
            job_id=job_id,
            draft_section_count=draft_n,
            spec_section_count=spec_n,
            rubric_criteria=rubric_snapshot,
            lifecycle_generation=claimed_generation,
        )
        _entry.bind_adapter(adapter)
        handle = None
        done: TurnDoneEvent | None = None
        try:

            async def _run() -> None:
                nonlocal handle, done
                # Pre-launch live-status recheck: a soft-delete/cancel may have flipped the
                # job to 'failed' between the read session and here. Don't spend tokens (or
                # send deleted-workspace content) on a job that's no longer running — this
                # narrows the delete-races-launch window. (Full prevention would need task
                # cancellation/signalling; finalize already won't resurrect a failed job.)
                with session_factory() as s2:
                    live = s2.get(EvaluatorJob, job_id)
                    if live is None or live.status != "running":
                        return
                # launch is INSIDE the wait_for so a hung SDK connect is bounded too — not
                # just the streaming turn. wait_for cancels this coroutine on timeout,
                # raising CancelledError at whichever await is live (launch or the loop).
                handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
                async for event in adapter.send_turn(handle, instruction):
                    if isinstance(event, TurnDoneEvent):
                        done = event  # outer; survives a later raise/cancel

            await asyncio.wait_for(_run(), timeout=settings.evaluator_pass_timeout_seconds)
        # Exception PLUS CancelledError, not BaseException. CancelledError is a
        # BaseException, so a cancelled pass used to skip _fail and lose its spend —
        # but catching BaseException wholesale would turn KeyboardInterrupt and
        # SystemExit into ordinary failed jobs. Name the two we mean; cancellation is
        # re-raised at the end of this handler.
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 — terminal
            logger.exception("evaluator pass failed for job %s", job_id)
            # Member-visible: surface only a sanitized, generic message (full detail
            # already in the logs above); distinguish the expected timeout outcome.
            message = (
                "Evaluation timed out."
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                else "Evaluation failed."
            )
            with session_factory() as s:
                # Lock the row + guard on status=='running': the separate reaper may
                # have already failed this job during a hang; don't double-finalize.
                job = s.get(EvaluatorJob, job_id, with_for_update=True)
                if job is not None and job.status != "running":
                    # The reaper or a cancel already finalized this job. Don't
                    # resurrect it — but the call still ran and still spent, and
                    # `_fail` below is skipped on this branch, so without this the
                    # spend is invisible to the cap. Same shape as the finalize
                    # block's late-spend path — including the column stamp, or the
                    # display cache reads 0 while the ledger reads the truth.
                    if done is not None:
                        job.input_tokens = done.tokens_in or 0
                        job.output_tokens = done.tokens_out or 0
                        job.model = done.model
                        job.sdk_session_id = done.session_id
                    record_spend(
                        s,
                        workspace_id=job.workspace_id,
                        kind="evaluator",
                        status="timeout"
                        if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                        else "failed",
                        tokens_in=done.tokens_in if done else None,
                        tokens_out=done.tokens_out if done else None,
                        model=done.model if done else None,
                        ref_id=job.id,
                        sdk_session_id=done.session_id if done else None,
                    )
                if job is not None and job.status == "running":
                    _fail(
                        s,
                        job,
                        message,
                        done=done,
                        status="timeout"
                        if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                        else "failed",
                    )
                s.commit()
            # Cancellation must still propagate — the caller's wait_for and the
            # scheduler both rely on it. We only widened the catch so the spend
            # above is recorded first.
            if isinstance(exc, asyncio.CancelledError):
                raise
            return
        finally:
            if handle is not None:
                try:
                    # Bound cleanup too: a hung SDK disconnect must not leak the pass
                    # coroutine (the scheduler has already stopped awaiting it). Kept well
                    # under the scheduler's pass-timeout future grace.
                    await asyncio.wait_for(
                        adapter.terminate(handle),
                        timeout=settings.evaluator_terminate_timeout_seconds,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("evaluator adapter terminate failed for job %s", job_id)

        # 2. Success finalize under a row lock; ONLY if still running AND a scorecard was
        #    written (the reaper may have failed it during a hang — don't resurrect it).
        with session_factory() as s:
            job = s.get(EvaluatorJob, job_id, with_for_update=True)
            if job is None:
                s.commit()
                return
            if job.status != "running":
                # The reaper or a cancel finalized this job mid-pass. Do NOT
                # resurrect it — but the sub-call still ran and still reported
                # usage, and after the cap moved to the ledger a `return` here
                # loses that spend entirely. uncited_claims already had this
                # path; coherence and evaluator did not, which is how an outside
                # review found it. Keep the display columns honest too.
                if done is not None:
                    job.input_tokens = done.tokens_in or 0
                    job.output_tokens = done.tokens_out or 0
                    job.model = done.model
                    job.sdk_session_id = done.session_id
                record_spend(
                    s,
                    workspace_id=job.workspace_id,
                    kind="evaluator",
                    status="failed",
                    tokens_in=done.tokens_in if done else None,
                    tokens_out=done.tokens_out if done else None,
                    model=done.model if done else None,
                    ref_id=job.id,
                    sdk_session_id=done.session_id if done else None,
                )
                s.commit()
                return
            if job.scorecard is None:
                _fail(s, job, "evaluator produced no scorecard", done=done)
                s.commit()
                return
            job.status = "completed"
            job.completed_at = datetime.now(UTC)
            if done is not None:
                job.input_tokens = done.tokens_in or 0
                job.output_tokens = done.tokens_out or 0
                job.model = done.model
                job.sdk_session_id = done.session_id
            record_spend(
                s,
                workspace_id=job.workspace_id,
                kind="evaluator",
                status="ok",
                tokens_in=done.tokens_in if done else None,
                tokens_out=done.tokens_out if done else None,
                model=done.model if done else None,
                ref_id=job.id,
                sdk_session_id=done.session_id if done else None,
            )
            s.commit()

async def drain_evaluator_queue_tick(
    *,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_evaluator_adapter,
) -> None:
    """Claim one job and run it. The reaper is a separate sync tick."""
    with session_factory() as s:
        job = claim_next_evaluator_job(s)
        s.commit()
        job_id = job.id if job is not None else None
        claimed_generation = job.claimed_at_generation if job is not None else None
    if job_id is None:
        return
    await run_evaluator_pass(
        job_id=job_id,
        claimed_generation=claimed_generation,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
