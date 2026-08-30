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
from call_manager.proposal_assist.passes import PASS_REGISTRY
from call_manager.proposal_assist.row_locks import bounded_prefix_lock_waits
from call_manager.proposal_assist.spend import record_spend
from call_manager.proposal_assist.turns.caps import check_global_daily_token_cap

logger = logging.getLogger(__name__)


async def run_coherence_pass(
    *,
    job_id: uuid.UUID,
    claimed_generation: int,
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
    # Tracked from the FIRST line, BEFORE the prefix: an entry registered
    # only after the prefix is invisible to a DELETE that commits during it.
    with session_factory() as s0:
        job0 = s0.get(CoherenceJob, job_id)
        if job0 is None or job0.status != "running":
            return
        tracked_ws = job0.workspace_id

    async with PASS_REGISTRY.track(tracked_ws) as _entry:
        # bounded_prefix_lock_waits wraps the WHOLE prefix transaction, commit
        # included: the workspace row is held from the acquisition below until this
        # session ends, across context assembly's filesystem reads. Without the bound
        # a stalled prefix pins the row against the owner's DELETE for as long as the
        # stall lasts — and that DELETE, past the commit mutex, now gives up on the row
        # after 2s and answers 429 having deleted nothing rather than hanging (Slice
        # C3). Giving up here costs a reaper requeue; not giving up costs the owner a
        # DELETE that fails outright.
        with session_factory() as s, bounded_prefix_lock_waits(s):
            job = s.get(CoherenceJob, job_id)
            if job is None or job.status != "running":
                return
            # Workspace ROW LOCK held through this session's commit. It linearizes the
            # prefix against a concurrent delete: the delete takes the same row to stamp
            # soft_deleted_at, so either we commit and it waits briefly, or it wins and
            # we abort having written nothing. Safe ONLY because this session is short.
            ws = s.get(Workspace, job.workspace_id, with_for_update=True)
            if ws is None or ws.soft_deleted_at is not None:
                return
            workspace_id = job.workspace_id
            requested_by = job.requested_by
            locked = ws.spec_locked_at is not None
            # The endpoint checked the GLOBAL cap when this job was enqueued; the
            # queue delay means that check is stale by now. Without a recheck here a
            # job accepted before the ceiling was reached still spends past it —
            # the same 'contributes to the total, never consults it' shape this
            # branch exists to remove. Checked before the per-family budget because
            # the org ceiling is the harder stop.
            if not check_global_daily_token_cap(s, settings=settings).allowed:
                _fail(s, job, "global daily token cap reached")
                s.commit()
                return
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
            lifecycle_generation=claimed_generation,
        )
        _entry.bind_adapter(adapter)
        handle = None
        terminate_cancelled = False
        spend_is_durable = False
        done: TurnDoneEvent | None = None
        try:

            async def _run() -> None:
                nonlocal handle, done
                # Pre-launch live-status recheck: a soft-delete/cancel may have flipped the
                # job to 'failed' between the read session and here. Don't spend tokens (or
                # send deleted-workspace content) on a job that's no longer running — this
                # narrows the delete-races-launch window. evaluator and uncited_claims have
                # had this since SP2b-2c; coherence did not, which is money spent on a job
                # nobody is waiting for.
                with session_factory() as s2:
                    live = s2.get(CoherenceJob, job_id)
                    if live is None or live.status != "running":
                        return
                # launch is INSIDE the wait_for so a hung SDK connect is bounded too — not
                # just the streaming turn. wait_for cancels this coroutine on timeout,
                # raising CancelledError at whichever await is live (launch or the loop).
                handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
                async for event in adapter.send_turn(handle, instruction):
                    if isinstance(event, TurnDoneEvent):
                        done = event  # outer; survives a later raise/cancel

            await asyncio.wait_for(_run(), timeout=settings.coherence_pass_timeout_seconds)
        # BaseException, deliberately — but the two concerns are separated rather
        # than traded off. Round 4 narrowed this to (Exception, CancelledError) so
        # KeyboardInterrupt and SystemExit would not become ordinary failed jobs;
        # round 8 pointed out that narrowing loses their spend, which is real money.
        # Both are satisfiable: RECORD on every BaseException, but only mark the job
        # `failed` for an Exception, and re-raise anything that is not one. A
        # shutdown signal leaves the job `running` for the reaper, with its spend on
        # the ledger.
        except BaseException as exc:  # noqa: BLE001 — terminal; non-Exception re-raised
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
                # `not isinstance(exc, Exception)` joins this branch: a shutdown
                # signal must record its spend WITHOUT flipping a still-running job
                # to failed — the reaper owns that job after the process dies.
                if job is not None and (
                    job.status != "running" or not isinstance(exc, Exception)
                ):
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
                        kind="coherence",
                        status="timeout"
                        if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                        else "failed",
                        tokens_in=done.tokens_in if done else None,
                        tokens_out=done.tokens_out if done else None,
                        model=done.model if done else None,
                        ref_id=job.id,
                        sdk_session_id=done.session_id if done else None,
                    )
                if (
                    job is not None
                    and job.status == "running"
                    and isinstance(exc, Exception)
                ):
                    _fail(
                        s,
                        job,
                        message,
                        done=done,
                        status="timeout"
                        if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                        else "failed",
                    )
                    supersede_own_coherence(s, workspace_id=workspace_id, job_id=job_id)
                s.commit()
            # Cancellation and shutdown must still propagate — the caller's wait_for
            # and the scheduler both rely on it. We only widened the catch so the
            # spend above is recorded first.
            spend_is_durable = True  # the handler above committed it
            if not isinstance(exc, Exception):
                raise
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
                # CancelledError too: a cancel landing during adapter teardown used to
                # escape the finally BEFORE the finalize block below, so a call that had
                # already completed and reported usage recorded nothing. Hold it, let the
                # ledger write happen, then re-raise it after the finalize.
                except (Exception, asyncio.CancelledError) as term_exc:  # noqa: BLE001
                    logger.exception("coherence adapter terminate failed for job %s", job_id)
                    if isinstance(term_exc, asyncio.CancelledError):
                        terminate_cancelled = True
            # If the handler above already committed and returned, that return
            # is IN FLIGHT: the flag check after the finalize block will never
            # run, so a teardown cancel has to escape from here. Safe only
            # because the spend is already durable on that path.
            if terminate_cancelled and spend_is_durable:
                raise asyncio.CancelledError()

        # 2. Success: claim the terminal transition under a row lock; ONLY finalize if
        #    the job is still 'running' (the reaper may have failed it during a hang —
        #    in which case its own cleanup already ran and we must not resurrect it).
        with session_factory() as s:
            from sqlalchemy import func, select

            from call_manager.db.models import ProposedChange

            job = s.get(CoherenceJob, job_id, with_for_update=True)
            if job is None:
                s.commit()
                # Held terminate-cancel must escape from EVERY finalize exit, not just the
                # fall-through one — that was the round-4 fix's own gap.
                if terminate_cancelled:
                    raise asyncio.CancelledError()
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
                    kind="coherence",
                    status="failed",
                    tokens_in=done.tokens_in if done else None,
                    tokens_out=done.tokens_out if done else None,
                    model=done.model if done else None,
                    ref_id=job.id,
                    sdk_session_id=done.session_id if done else None,
                )
                s.commit()
                # Held terminate-cancel must escape from EVERY finalize exit, not just the
                # fall-through one — that was the round-4 fix's own gap.
                if terminate_cancelled:
                    raise asyncio.CancelledError()
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
            record_spend(
                s,
                workspace_id=job.workspace_id,
                kind="coherence",
                status="ok",
                tokens_in=done.tokens_in if done else None,
                tokens_out=done.tokens_out if done else None,
                model=done.model if done else None,
                ref_id=job.id,
                sdk_session_id=done.session_id if done else None,
            )
            s.commit()

        # A cancel that landed during adapter teardown was held so the ledger write
        # above could happen. The spend is durable now; let it propagate.
        if terminate_cancelled:
            raise asyncio.CancelledError()

def _fail(
    session, job: CoherenceJob, message: str, done=None, status: str = "failed"
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
        kind="coherence",
        status=status,
        tokens_in=done.tokens_in if done else None,
        tokens_out=done.tokens_out if done else None,
        model=done.model if done else None,
        ref_id=job.id,
        sdk_session_id=done.session_id if done else None,
    )
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
        claimed_generation = job.claimed_at_generation if job is not None else None
    if job_id is None:
        return
    await run_coherence_pass(
        job_id=job_id,
        claimed_generation=claimed_generation,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
