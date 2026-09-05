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

from grant_lab.config import Settings
from grant_lab.db.models import ReviewRun, ReviewRunItem, Workspace
from grant_lab.proposal_assist.citations.gitio import (
    GitCommitError,
    blob_sha,
    write_and_commit_files,
)
from grant_lab.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent

# CommitMutexTimeout and WorkspaceDeleted come from drafting/locking.py, their
# actual home. They used to be reachable through fencing only as a side effect of
# a module-level import there, which the 2026-09-05 import-cycle fix deferred;
# fencing's own __all__ never listed them.
from grant_lab.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    WorkspaceDeleted,
)
from grant_lab.proposal_assist.fencing import (
    WorkspaceGenerationStale,
    acquire_commit_mutex_at_generation,
)
from grant_lab.proposal_assist.passes import PASS_REGISTRY
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.ports.stamp import FenceStamp
from grant_lab.proposal_assist.review.agent import build_review_adapter
from grant_lab.proposal_assist.review.context import (
    assemble_review_context,
    consolidation_instruction,
    persona_instruction,
)
from grant_lab.proposal_assist.review.jobs import (
    claim_next_review_run,
    estimate_dollars,
    review_dollar_cap,
    review_dollars_today,
    review_run_cap,
    review_runs_today,
    rollup_run_totals,
)
from grant_lab.proposal_assist.review.personas import (
    PERSONA_KINDS,
    REVIEW_KINDS,
    WAVE1_KINDS,
)
from grant_lab.proposal_assist.row_locks import bounded_prefix_lock_waits
from grant_lab.proposal_assist.spend import record_spend
from grant_lab.proposal_assist.turns.caps import check_global_daily_token_cap

logger = logging.getLogger(__name__)

_FIT_CHECK_FILE = "fit-check.md"
_RUBRIC_REVIEW_FILE = "rubric-review.md"


class _GlobalCapReached(Exception):
    """Raised inside a review sub-call when the org ceiling is already reached.

    An Exception on purpose: the existing handler records the (zero-token) spend,
    marks the item failed and never launches, which is exactly the wanted
    behaviour — no special-casing needed anywhere else.
    """


def _now() -> datetime:
    return datetime.now(UTC)


async def _run_one_call(
    *, kind, instruction, run_id, workspace_id, session_factory, settings, adapter_builder,
    lifecycle_generation
) -> None:
    """Run a single sub-call against its (already 'running') item. Records tokens +
    dollars and flips the item to done/failed. Never raises — failure is recorded."""
    # Tracked from the FIRST line, prefix included: an entry that appears only
    # after the prefix is invisible to a DELETE that commits during it.
    async with PASS_REGISTRY.track(workspace_id) as _entry:
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
            lifecycle_generation=lifecycle_generation,
        )
        _entry.bind_adapter(adapter)
        handle = None
        terminate_cancelled = False
        done: TurnDoneEvent | None = None
        error: str | None = None
        cancelled = False
        try:

            async def _drive() -> None:
                nonlocal handle, done
                # The pass-start recheck happens ONCE, before five concurrent
                # wave-1 calls. Consolidation then runs after those five have
                # already committed their spend, and a semaphore-queued wave-1
                # call can launch long after the check too. So the ceiling is
                # tested here, immediately before this sub-call's own launch.
                with session_factory() as cap_s:
                    if not check_global_daily_token_cap(cap_s, settings=settings).allowed:
                        raise _GlobalCapReached
                handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
                async for event in adapter.send_turn(handle, instruction):
                    if isinstance(event, TurnDoneEvent):
                        done = event  # outer; survives a later raise/cancel

            await asyncio.wait_for(_drive(), timeout=settings.review_persona_timeout_seconds)
        # Exception PLUS CancelledError, not BaseException. CancelledError is a
        # BaseException, so a cancelled sub-call used to skip the finalize block below
        # and lose its spend — but catching BaseException wholesale would turn
        # KeyboardInterrupt and SystemExit into ordinary failed sub-calls. We fall
        # through, record, and re-raise cancellation afterwards.
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 — terminal
            logger.exception("review sub-call %s failed for run %s", kind, run_id)
            cancelled = isinstance(exc, asyncio.CancelledError)
            if isinstance(exc, _GlobalCapReached):
                error = "global daily token cap reached"
            elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                error = "timed out"
            else:
                error = "sub-call failed"
        finally:
            if handle is not None:
                try:
                    await asyncio.wait_for(
                        adapter.terminate(handle),
                        timeout=settings.review_terminate_timeout_seconds,
                    )
                # CancelledError too: a cancel landing during adapter teardown used to
                # escape the finally BEFORE the finalize block below, so a call that had
                # already completed and reported usage recorded nothing. Hold it, let the
                # ledger write happen, then re-raise it after the finalize.
                except (Exception, asyncio.CancelledError) as term_exc:  # noqa: BLE001
                    logger.exception("review adapter terminate failed (%s, run %s)", kind, run_id)
                    if isinstance(term_exc, asyncio.CancelledError):
                        terminate_cancelled = True

        with session_factory() as s:
            # LOCK ORDER: review_runs < review_run_items (normative). The run is taken
            # FIRST even though the item is what we came to write: the child prefix
            # above takes run-then-item, and the original item-then-run order here was
            # an AB-BA inversion against it (and against the delete path, which also
            # takes runs then items).
            run = s.get(ReviewRun, run_id, with_for_update=True)
            item = s.get(ReviewRunItem, item_id, with_for_update=True)
            if item is None:
                # The item was deleted while its sub-call was in flight. There is
                # no row left to stamp, but the hosted call still happened and
                # still cost money, so the ledger must carry it or the spend is
                # invisible to the cap. `ref_id` stays the item id even though the
                # item is gone — it is the call's identity, not a foreign key.
                record_spend(
                    s,
                    workspace_id=workspace_id,
                    kind="review",
                    status="timeout" if error == "timed out" else "failed",
                    tokens_in=done.tokens_in if done else None,
                    tokens_out=done.tokens_out if done else None,
                    model=done.model if done else None,
                    ref_id=item_id,
                    sdk_session_id=done.session_id if done else None,
                )
                s.commit()
                # This early return sits ABOVE the re-raise at the end of the
                # function, so cancellation would be swallowed here. The spend is
                # durable now; let it propagate.
                if cancelled or terminate_cancelled:
                    raise asyncio.CancelledError()
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
            elif (
                error is None
                and item.raw_response_json
                and item.raw_response_json.get("markdown")
            ):
                item.status = "done"
            else:
                item.status = "failed"
                item.error_message = error or "no review submitted"
            # One row per sub-call. `status` reads the item's settled terminal
            # state, plus the timeout the except block at :132-138 already
            # distinguished and stored in `error`.
            record_spend(
                s,
                workspace_id=workspace_id,
                kind="review",
                status=(
                    "timeout"
                    if error == "timed out"
                    else "ok"
                    if item.status == "done"
                    else "failed"
                ),
                tokens_in=done.tokens_in if done else None,
                tokens_out=done.tokens_out if done else None,
                model=done.model if done else None,
                ref_id=item.id,
            )
            s.commit()

        # The spend is now durable; cancellation may resume propagating. Re-raised
        # AFTER the finalize block (not inside the handler) so the ledger row is
        # written first — that ordering is the whole point of widening the catch.
        if cancelled or terminate_cancelled:
            raise asyncio.CancelledError()


def _markdown_of(session, run_id, kind) -> str | None:
    item = session.scalar(
        select(ReviewRunItem).where(
            ReviewRunItem.review_run_id == run_id, ReviewRunItem.kind == kind
        )
    )
    if item and item.status == "done" and item.raw_response_json:
        return item.raw_response_json.get("markdown")
    return None



def _fail_unfenced_run(session_factory, *, run_id: uuid.UUID, reason: str) -> None:
    """Terminalize a run whose finalize was refused by the fence.

    A SEPARATE session, because the refusal rolled the fenced one back — and the
    mutex is released at that ROLLBACK, so this transaction is not holding it and
    cannot self-deadlock. Deliberately DB-only: the whole point of the refusal is
    that nothing may be written to git for a workspace that has moved on.

    Conditional on status='running' so it cannot resurrect a row the delete path
    or the reaper already terminalized while the six hosted calls were in flight.
    """
    with session_factory() as s:
        run = s.get(ReviewRun, run_id, with_for_update=True)
        if run is None or run.status != "running":
            s.commit()
            return
        for it in s.scalars(select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)):
            if it.status == "running":
                it.status = "failed"
            if it.kind == "consolidation":
                it.error_message = f"workspace moved on before finalize: {reason}"
        run.status = "failed"
        run.completed_at = _now()
        s.commit()


async def run_review_pass(
    *,
    run_id: uuid.UUID,
    stamp: FenceStamp,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_review_adapter,
) -> None:
    # Tracked from the FIRST line, BEFORE the prefix. Task-only: run_review_pass
    # owns NO adapter — every _run_one_call builds its own — so retirement
    # cancels this task and there is nothing here to retire.
    with session_factory() as s0:
        run0 = s0.get(ReviewRun, run_id)
        if run0 is None or run0.status != "running":
            return
        tracked_ws = run0.workspace_id

    # The stamp names a workspace and so does the run. Nothing downstream checks
    # they agree, and `checkpoint_artifact` has the same latent split: it LOCKS
    # stamp.workspace_id while READING and WRITING a separately-supplied
    # workspace_id. A mismatch would fence one workspace and commit into another,
    # which is precisely the failure the fence exists to prevent — so it is an
    # assert, not a convention.
    if stamp.workspace_id != tracked_ws:
        raise ValueError(
            f"stamp names workspace {stamp.workspace_id} but review run {run_id} "
            f"belongs to {tracked_ws}"
        )

    async with PASS_REGISTRY.track(tracked_ws):
        # 1. Short read session: guard running, pre-create the 6 items FIRST, pin context,
        #    set draft_sha, re-check caps.
        # bounded_prefix_lock_waits wraps the WHOLE prefix transaction, commit included:
        # the workspace row is held from the acquisition below until COMMIT, across
        # context assembly's filesystem reads and the item-row INSERTs. Without the bound
        # a stalled prefix pins the row against the owner's DELETE for as long as the
        # stall lasts — and that DELETE, past the commit mutex, now gives up on the row
        # after 2s and answers 429 having deleted nothing rather than hanging (Slice
        # C3). Bounding the prefix is what keeps the owner's DELETE from failing at
        # all. See proposal_assist/row_locks.py.
        with session_factory() as s, bounded_prefix_lock_waits(s):
            run = s.get(ReviewRun, run_id)
            if run is None or run.status != "running":
                return
            workspace_id = run.workspace_id
            # Workspace ROW LOCK held through this session's commit — the same guard the
            # other three runners have. This prefix commits DURABLE progress (the
            # ReviewRunItem rows and run.draft_sha), so without it those are writes
            # against a deleted workspace.
            ws = s.get(Workspace, workspace_id, with_for_update=True)
            if ws is None or ws.soft_deleted_at is not None:
                return
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
            # pass-start cap recheck (a long queue wait may have pushed the ws over
            # budget). The GLOBAL ceiling joins the same recheck: the endpoint tested
            # it at enqueue time and that answer is stale by now, so without this a
            # run accepted before the org hit its ceiling still spends past it — six
            # hosted calls' worth, in this family's case.
            over = (
                not check_global_daily_token_cap(s, settings=settings).allowed
                or review_runs_today(s, workspace_id=workspace_id) > review_run_cap(ws, settings)
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
                    lifecycle_generation=stamp.generation,
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
                lifecycle_generation=stamp.generation,
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

        # 4. Write artifacts + finalize (mutex-fenced, row-locked, still-running).
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        with session_factory() as s:
            # LOCK ORDER: the mutex (M) is taken BEFORE the ReviewRun row (J).
            # Putting it after the FOR UPDATE below would make this finalize
            # J->M while the delete path runs M->J — a real 40P01, which is
            # exactly what fencing.py:139-145 forbids.
            #
            # This is the only production git-commit sink that took no mutex and
            # no fence at all, and `git commit` carries NO PATHSPEC
            # (gitio.py:198): it commits the whole index. So an unfenced writer
            # here does not merely commit to the wrong branch — it can hand the
            # next checkpoint a commit it did not author. The six hosted calls
            # above take minutes, and a switch, delete or retirement can land in
            # that window; the generation check is what notices.
            #
            # Safe to take here: this runner holds no shared lease (the upgrade
            # deadlock the protocol warns about needs one), and every LLM call is
            # already finished, so the mutex is never held across one.
            try:
                acquire_commit_mutex_at_generation(
                    s, workspace_id, generation=stamp.generation
                )
            except (WorkspaceDeleted, WorkspaceGenerationStale, CommitMutexTimeout) as exc:
                logger.warning(
                    "review finalize refused for run %s: %s", run_id, exc,
                    extra={"event": "review_finalize_fenced", "review_run_id": str(run_id)},
                )
                s.rollback()
                _fail_unfenced_run(session_factory, run_id=run_id, reason=str(exc))
                # DELIBERATELY REDUNDANT, and a mutation cannot prove it: deleting
                # this `return` leaves all tests green, because _fail_unfenced_run
                # has just flipped the row out of 'running' and the status check
                # below then returns anyway. It stays because the rollback above
                # RELEASED the mutex — falling through would reach the git write
                # holding nothing, and the only thing stopping it would be a guard
                # fifteen lines away that exists for a different reason. Two doors
                # on the same exit is the point.
                return
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
        # The stamp is built HERE, from the values the claiming statement itself
        # captured (F3a claim identity), not re-read later. `origin_job_id` is the
        # review run: it IS this actor's own background job row, which is the role
        # FenceStamp documents for the submit_* effects.
        stamp = (
            None
            if run is None
            else FenceStamp(
                workspace_id=run.workspace_id,
                generation=run.claimed_at_generation,
                active_turn_job_id=None,
                origin_job_id=run.id,
            )
        )
        run_id = run.id if run is not None else None
    if run_id is None or stamp is None:
        return
    await run_review_pass(
        run_id=run_id,
        stamp=stamp,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
