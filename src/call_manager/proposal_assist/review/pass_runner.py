"""Run one mock-panel review pass (the 6-call fan-out) for a claimed (running)
review_runs row, and the async drain tick. Bespoke (unlike the single-call
coherence/evaluator passes): Wave 1 = fit-check + 4 personas concurrently
(semaphore-bounded), Wave 2 = consolidation over the survivors. Each sub-call is
its own one-shot adapter with ONLY submit_review; the produced markdown is read
back from the item's raw_response_json. Partial-failure aware. Carries the
evaluator hardening: launch inside wait_for, bounded terminate, FOR UPDATE
finalize guard."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, ReviewRunItem, Workspace
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    blob_sha,
    write_and_commit_files,
)
from call_manager.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.review.agent import build_review_adapter
from call_manager.proposal_assist.review.context import (
    assemble_review_context,
    consolidation_instruction,
    persona_instruction,
)
from call_manager.proposal_assist.review.jobs import (
    claim_next_review_run,
    estimate_dollars,
    review_dollar_cap,
    review_dollars_today,
    review_run_cap,
    review_runs_today,
    rollup_run_totals,
)
from call_manager.proposal_assist.review.personas import (
    PERSONA_KINDS,
    REVIEW_KINDS,
    WAVE1_KINDS,
)

logger = logging.getLogger(__name__)

_FIT_CHECK_FILE = "fit-check.md"
_RUBRIC_REVIEW_FILE = "rubric-review.md"


def _now() -> datetime:
    return datetime.now(UTC)


async def _run_one_call(
    *, kind, instruction, run_id, workspace_id, session_factory, settings, adapter_builder
) -> None:
    """Run a single sub-call against its (already 'running') item. Records tokens +
    dollars and flips the item to done/failed. Never raises — failure is recorded."""
    with session_factory() as s:
        # LOCK ORDER: review_runs < review_run_items (normative — see the matching
        # note in the finalization session below). The bare read this replaces was a
        # TOCTOU window: a DELETE cancels review runs through
        # cancel_review_runs_and_items_for_workspace and could land between the
        # status check and the `item.status = "running"` commit, stranding the item
        # in 'running' on a deleted workspace.
        run = s.get(ReviewRun, run_id, with_for_update=True)
        item = s.scalar(
            select(ReviewRunItem).where(
                ReviewRunItem.review_run_id == run_id, ReviewRunItem.kind == kind
            )
        )
        # Defensive early return (B9 defect 6): the "never raises" contract means
        # we must not assume the item row exists. If the run's items were never
        # created (or were pruned), bail instead of dereferencing item.id -> None.
        if item is None:
            return
        item_id = item.id
        # Pre-launch live recheck (mirrors evaluator/pass_runner.py:92): a soft-delete
        # cancel or reaper may have flipped the run between step-1's read session and
        # now — and with the wave-1 semaphore, a queued sub-call can START long after
        # wave-1 began. Don't spend tokens or send workspace content for a run that is
        # no longer running. Full prevention would need task cancellation; this narrows
        # the window the way the evaluator does.
        if run is None or run.status != "running":
            item.status = "cancelled"
            item.error_message = "run no longer active"
            item.completed_at = _now()
            s.commit()
            return
        item.status = "running"
        item.started_at = _now()
        item.model = settings.proposal_assist_llm_model
        s.commit()

    adapter = adapter_builder(
        settings=settings,
        session_factory=session_factory,
        workspace_id=workspace_id,
        review_run_id=run_id,
        item_id=item_id,
        kind=kind,
    )
    handle = None
    done: TurnDoneEvent | None = None
    error: str | None = None
    try:

        async def _drive() -> TurnDoneEvent | None:
            nonlocal handle
            handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
            last = None
            async for event in adapter.send_turn(handle, instruction):
                if isinstance(event, TurnDoneEvent):
                    last = event
            return last

        done = await asyncio.wait_for(_drive(), timeout=settings.review_persona_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — terminal for THIS sub-call
        logger.exception("review sub-call %s failed for run %s", kind, run_id)
        error = (
            "timed out"
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            else "sub-call failed"
        )
    finally:
        if handle is not None:
            try:
                await asyncio.wait_for(
                    adapter.terminate(handle),
                    timeout=settings.review_terminate_timeout_seconds,
                )
            except Exception:  # noqa: BLE001
                logger.exception("review adapter terminate failed (%s, run %s)", kind, run_id)

    with session_factory() as s:
        # LOCK ORDER: review_runs < review_run_items (normative). The run is taken
        # FIRST even though the item is what we came to write: the child prefix
        # above takes run-then-item, and the original item-then-run order here was
        # an AB-BA inversion against it (and against the delete path, which also
        # takes runs then items).
        run = s.get(ReviewRun, run_id, with_for_update=True)
        item = s.get(ReviewRunItem, item_id, with_for_update=True)
        if item is None:
            return
        item.completed_at = _now()
        if done is not None:
            item.input_tokens = done.tokens_in or 0
            item.output_tokens = done.tokens_out or 0
            item.estimated_dollars = estimate_dollars(
                done.tokens_in or 0, done.tokens_out or 0, settings
            )
        if run is None or run.status != "running":
            # The reaper (TTL) or a soft-delete cancel finalized the run mid-call.
            # Mirror the pre-launch recheck — don't resurrect the run's item to done
            # — but keep the tokens this sub-call spent and re-roll the run total so
            # the late cost is still counted by review_dollars_today.
            item.status = "cancelled"
            item.error_message = "run no longer active"
            if run is not None:
                rollup_run_totals(
                    run,
                    s.scalars(
                        select(ReviewRunItem).where(
                            ReviewRunItem.review_run_id == run_id
                        )
                    ).all(),
                )
        elif error is None and item.raw_response_json and item.raw_response_json.get("markdown"):
            item.status = "done"
        else:
            item.status = "failed"
            item.error_message = error or "no review submitted"
        s.commit()


def _markdown_of(session, run_id, kind) -> str | None:
    item = session.scalar(
        select(ReviewRunItem).where(
            ReviewRunItem.review_run_id == run_id, ReviewRunItem.kind == kind
        )
    )
    if item and item.status == "done" and item.raw_response_json:
        return item.raw_response_json.get("markdown")
    return None


async def run_review_pass(
    *,
    run_id: uuid.UUID,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_review_adapter,
) -> None:
    # 1. Short read session: guard running, pre-create the 6 items FIRST, pin context,
    #    set draft_sha, re-check caps.
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        if run is None or run.status != "running":
            return
        workspace_id = run.workspace_id
        ws = s.get(Workspace, workspace_id)
        existing = {
            i.kind
            for i in s.scalars(
                select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
            )
        }
        for kind in REVIEW_KINDS:
            if kind not in existing:
                s.add(ReviewRunItem(review_run_id=run_id, kind=kind, status="queued"))
        ctx = assemble_review_context(
            session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
        )
        run.draft_sha = ctx.draft_sha
        # pass-start cap recheck (a long queue wait may have pushed the ws over budget)
        over = (
            review_runs_today(s, workspace_id=workspace_id) > review_run_cap(ws, settings)
            or review_dollars_today(s, workspace_id=workspace_id)
            >= review_dollar_cap(ws, settings)
        )
        if over:
            for it in s.scalars(
                select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
            ):
                it.status = "skipped"
                if it.kind == "consolidation":
                    it.error_message = "daily mock-panel budget reached"
            run.status = "failed"
            run.completed_at = _now()
            s.commit()
            return
        s.commit()

    # 2. Wave 1 — fit-check + 4 personas concurrently (semaphore-bounded).
    sem = asyncio.Semaphore(settings.review_persona_concurrency)

    async def _guarded(kind: str, instruction: str) -> None:
        async with sem:
            await _run_one_call(
                kind=kind,
                instruction=instruction,
                run_id=run_id,
                workspace_id=workspace_id,
                session_factory=session_factory,
                settings=settings,
                adapter_builder=adapter_builder,
            )

    wave1 = [_guarded(kind, persona_instruction(kind, ctx)) for kind in WAVE1_KINDS]
    await asyncio.gather(*wave1)  # each _run_one_call swallows its own failure

    # 3. Wave 2 — consolidation over whatever personas succeeded.
    with session_factory() as s:
        persona_md = {k: _markdown_of(s, run_id, k) for k in PERSONA_KINDS}
        persona_md = {k: v for k, v in persona_md.items() if v}
        fit_md = _markdown_of(s, run_id, "fit_check")
    if persona_md:
        cons_instr = consolidation_instruction(ctx, persona_md, fit_md)
        await _run_one_call(
            kind="consolidation",
            instruction=cons_instr,
            run_id=run_id,
            workspace_id=workspace_id,
            session_factory=session_factory,
            settings=settings,
            adapter_builder=adapter_builder,
        )
    else:
        with session_factory() as s:
            it = s.scalar(
                select(ReviewRunItem).where(
                    ReviewRunItem.review_run_id == run_id,
                    ReviewRunItem.kind == "consolidation",
                )
            )
            it.status = "skipped"
            it.error_message = "all personas failed"
            it.completed_at = _now()
            s.commit()

    # 4. Write artifacts + finalize (row-locked, still-running guard).
    repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
    with session_factory() as s:
        run = s.get(ReviewRun, run_id, with_for_update=True)
        if run is None or run.status != "running":
            s.commit()
            return
        items = {
            i.kind: i
            for i in s.scalars(
                select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
            )
        }
        fit_md = _markdown_of(s, run_id, "fit_check")
        cons_md = _markdown_of(s, run_id, "consolidation")
        files: dict[str, str] = {}
        if fit_md:
            files[_FIT_CHECK_FILE] = fit_md
        if cons_md:
            files[_RUBRIC_REVIEW_FILE] = cons_md
        commit_failed = False
        if files:
            try:
                write_and_commit_files(repo_dir, files, f"mock-panel review {run_id}")
                if _FIT_CHECK_FILE in files:
                    run.fit_check_sha = blob_sha(repo_dir, "HEAD", _FIT_CHECK_FILE)
                if _RUBRIC_REVIEW_FILE in files:
                    run.rubric_review_sha = blob_sha(repo_dir, "HEAD", _RUBRIC_REVIEW_FILE)
            except GitCommitError:
                # Don't let a git failure escape the finalize and wedge the active
                # slot until the reaper TTL — fail the run NOW, surface it on the item.
                logger.exception("review artifact commit failed for run %s", run_id)
                commit_failed = True
                cons = items.get("consolidation")
                if cons is not None:
                    cons.error_message = (
                        (cons.error_message or "") + " [artifact commit failed]"
                    ).strip()
        rollup_run_totals(run, items.values())
        all_done = all(items[k].status == "done" for k in REVIEW_KINDS if k in items)
        cons_done = items.get("consolidation") and items["consolidation"].status == "done"
        if commit_failed:
            run.status = "failed"
        elif all_done:
            run.status = "done"
        elif cons_done:
            run.status = "partial"
        else:
            run.status = "failed"
        run.completed_at = _now()
        s.commit()


async def drain_review_queue_tick(
    *,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_review_adapter,
) -> None:
    with session_factory() as s:
        run = claim_next_review_run(s)
        s.commit()
        run_id = run.id if run is not None else None
    if run_id is None:
        return
    await run_review_pass(
        run_id=run_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
