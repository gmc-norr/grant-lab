"""Run one uncited-claims pass for a claimed (running) uncited_claims_jobs row, and the
async drain tick. Fresh-stateless: a dedicated one-shot adapter, no resume, terminated in
finally. The submit_uncited_claims tool writes the RAW list; this runner applies the
verbatim-substring filter + hashing + dedupe at finalize (it holds the pinned section texts).
Carries the evaluator hardening: launch+drive bounded together, bounded terminate, FOR UPDATE
finalize guard. The separate sync reaper handles crash recovery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob, Workspace
from call_manager.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent
from call_manager.proposal_assist.passes import PASS_REGISTRY
from call_manager.proposal_assist.row_locks import bounded_prefix_lock_waits
from call_manager.proposal_assist.spend import record_spend
from call_manager.proposal_assist.uncited_claims.agent import build_uncited_claims_adapter
from call_manager.proposal_assist.uncited_claims.context import assemble_uncited_claims_context
from call_manager.proposal_assist.uncited_claims.jobs import (
    claim_next_uncited_claims_job,
    uncited_claims_cap_exceeded,
)

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


def _fail(
    session, job: UncitedClaimsJob, message: str, done=None, status: str = "failed"
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
        kind="uncited_claims",
        status=status,
        tokens_in=done.tokens_in if done else None,
        tokens_out=done.tokens_out if done else None,
        model=done.model if done else None,
        ref_id=job.id,
        sdk_session_id=done.session_id if done else None,
    )
    session.flush()


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def _finalize_claims(raw: list[dict], section_texts: dict[int, str]) -> list[dict]:
    """Keep a claim only if its claim_text is a VERBATIM substring of its referenced pinned
    section (drops hallucinated/paraphrased text); compute claim_hash server-side; dedupe by
    hash. The model is never trusted to supply the hash."""
    out: list[dict] = []
    seen: set[str] = set()
    for c in raw or []:
        text = c.get("claim_text") or ""
        si = c.get("section_index")
        section = section_texts.get(si, "")
        if not text or text not in section:
            continue
        h = hashlib.sha256(_normalize(text).encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(
            {
                "claim_text": text,
                "section_index": si,
                "reason": c.get("reason") or "",
                "claim_hash": h,
            }
        )
    return out


async def run_uncited_claims_pass(
    *,
    job_id: uuid.UUID,
    claimed_generation: int,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_uncited_claims_adapter,
) -> None:
    # 1. Short read session: assemble context (pinned draft), capture staleness marker.
    # Tracked from the FIRST line, BEFORE the prefix: an entry registered
    # only after the prefix is invisible to a DELETE that commits during it.
    with session_factory() as s0:
        job0 = s0.get(UncitedClaimsJob, job_id)
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
            job = s.get(UncitedClaimsJob, job_id)
            if job is None or job.status != "running":
                return
            # Workspace ROW LOCK held through this session's commit — this prefix
            # commits evaluated_draft_sha, so without the guard that is a durable
            # write against a deleted workspace.
            ws = s.get(Workspace, job.workspace_id, with_for_update=True)
            if ws is None or ws.soft_deleted_at is not None:
                return
            workspace_id = job.workspace_id
            requested_by = job.requested_by
            if uncited_claims_cap_exceeded(s, workspace_id=workspace_id, settings=settings):
                _fail(s, job, "daily uncited-claims budget reached")
                s.commit()
                return
            ctx = assemble_uncited_claims_context(
                session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
            )
            job.evaluated_draft_sha = ctx.draft_sha
            instruction = ctx.instruction
            draft_n = ctx.draft_section_count
            section_texts = ctx.section_texts
            s.commit()

        adapter = adapter_builder(
            settings=settings,
            session_factory=session_factory,
            workspace_id=workspace_id,
            requested_by=requested_by,
            job_id=job_id,
            draft_section_count=draft_n,
            lifecycle_generation=claimed_generation,
        )
        _entry.bind_adapter(adapter)
        handle = None
        done: TurnDoneEvent | None = None
        try:

            async def _run() -> None:
                nonlocal handle, done
                # Pre-launch live-status recheck: a soft-delete/cancel may have flipped the job to
                # 'failed' between the read session and here. Don't spend tokens on a dead job.
                with session_factory() as s2:
                    live = s2.get(UncitedClaimsJob, job_id)
                    if live is None or live.status != "running":
                        return
                handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
                async for event in adapter.send_turn(handle, instruction):
                    if isinstance(event, TurnDoneEvent):
                        done = event  # outer; survives a later raise/cancel

            await asyncio.wait_for(
                _run(), timeout=settings.uncited_claims_pass_timeout_seconds
            )
        # Exception PLUS CancelledError, not BaseException. CancelledError is a
        # BaseException, so a cancelled pass used to skip _fail and lose its spend —
        # but catching BaseException wholesale would turn KeyboardInterrupt and
        # SystemExit into ordinary failed jobs. Name the two we mean; cancellation is
        # re-raised at the end of this handler.
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 — terminal
            logger.exception("uncited-claims pass failed for job %s", job_id)
            message = (
                "Uncited-claims check timed out."
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                else "Uncited-claims check failed."
            )
            with session_factory() as s:
                job = s.get(UncitedClaimsJob, job_id, with_for_update=True)
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
                        kind="uncited_claims",
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
                    await asyncio.wait_for(
                        adapter.terminate(handle),
                        timeout=settings.uncited_claims_terminate_timeout_seconds,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("uncited-claims adapter terminate failed for job %s", job_id)

        # 2. Finalize under a row lock. ALWAYS record token/session accounting when a
        #    TurnDoneEvent arrived — even if the reaper/cancel already failed the job
        #    mid-pass — so late spend still counts toward the daily cap (spec §3: "don't
        #    resurrect it; still roll up tokens", mirroring review/pass_runner.py). Only
        #    status/claims/completed_at are gated on the job still running.
        with session_factory() as s:
            job = s.get(UncitedClaimsJob, job_id, with_for_update=True)
            if job is None:
                s.commit()
                return
            if done is not None:
                job.input_tokens = done.tokens_in or 0
                job.output_tokens = done.tokens_out or 0
                job.model = done.model
                job.sdk_session_id = done.session_id
            if job.status != "running":
                # The reaper or a cancel finalized the job mid-pass. The sub-call
                # still ran and still reported usage — the comment above says to
                # keep it — so the ledger must carry it too, or Task 7's cap will
                # not see spend the display columns just recorded.
                record_spend(
                    s,
                    workspace_id=job.workspace_id,
                    kind="uncited_claims",
                    status="failed",
                    tokens_in=done.tokens_in if done else None,
                    tokens_out=done.tokens_out if done else None,
                    model=done.model if done else None,
                    ref_id=job.id,
                    sdk_session_id=done.session_id if done else None,
                )
                s.commit()
                return
            if job.claims is None:
                _fail(s, job, "uncited-claims pass produced no result", done=done)
                s.commit()
                return
            job.claims = _finalize_claims(job.claims, section_texts)
            job.status = "completed"
            job.completed_at = datetime.now(UTC)
            record_spend(
                s,
                workspace_id=job.workspace_id,
                kind="uncited_claims",
                status="ok",
                tokens_in=done.tokens_in if done else None,
                tokens_out=done.tokens_out if done else None,
                model=done.model if done else None,
                ref_id=job.id,
                sdk_session_id=done.session_id if done else None,
            )
            s.commit()

async def drain_uncited_claims_queue_tick(
    *,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_uncited_claims_adapter,
) -> None:
    """Claim one job and run it. The reaper is a separate sync tick."""
    with session_factory() as s:
        job = claim_next_uncited_claims_job(s)
        s.commit()
        job_id = job.id if job is not None else None
        claimed_generation = job.claimed_at_generation if job is not None else None
    if job_id is None:
        return
    await run_uncited_claims_pass(
        job_id=job_id,
        claimed_generation=claimed_generation,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
