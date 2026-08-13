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
from call_manager.proposal_assist.uncited_claims.agent import build_uncited_claims_adapter
from call_manager.proposal_assist.uncited_claims.context import assemble_uncited_claims_context
from call_manager.proposal_assist.uncited_claims.jobs import (
    claim_next_uncited_claims_job,
    uncited_claims_cap_exceeded,
)

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


def _fail(session, job: UncitedClaimsJob, message: str) -> None:
    job.status = "failed"
    job.error = message
    job.completed_at = datetime.now(UTC)
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
        with session_factory() as s:
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
        )
        _entry.bind_adapter(adapter)
        handle = None
        done: TurnDoneEvent | None = None
        try:

            async def _run() -> TurnDoneEvent | None:
                nonlocal handle
                # Pre-launch live-status recheck: a soft-delete/cancel may have flipped the job to
                # 'failed' between the read session and here. Don't spend tokens on a dead job.
                with session_factory() as s2:
                    live = s2.get(UncitedClaimsJob, job_id)
                    if live is None or live.status != "running":
                        return None
                handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
                last = None
                async for event in adapter.send_turn(handle, instruction):
                    if isinstance(event, TurnDoneEvent):
                        last = event
                return last

            done = await asyncio.wait_for(
                _run(), timeout=settings.uncited_claims_pass_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 — terminal: fail the job, never propagate
            logger.exception("uncited-claims pass failed for job %s", job_id)
            message = (
                "Uncited-claims check timed out."
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                else "Uncited-claims check failed."
            )
            with session_factory() as s:
                job = s.get(UncitedClaimsJob, job_id, with_for_update=True)
                if job is not None and job.status == "running":
                    _fail(s, job, message)
                s.commit()
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
                # The reaper (TTL) or a soft-delete cancel finalized the job mid-pass. Don't
                # resurrect it (no status/claims change) — but keep the tokens recorded above.
                s.commit()
                return
            if job.claims is None:
                _fail(s, job, "uncited-claims pass produced no result")
                s.commit()
                return
            job.claims = _finalize_claims(job.claims, section_texts)
            job.status = "completed"
            job.completed_at = datetime.now(UTC)
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
    if job_id is None:
        return
    await run_uncited_claims_pass(
        job_id=job_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
