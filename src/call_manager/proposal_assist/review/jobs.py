"""Pure-DB helpers for the review_runs queue + the daily caps + token→dollar pricing.
The caller owns the transaction (these flush, never commit), mirroring
evaluator/jobs.py. Bespoke: no started_at column on ReviewRun (the reaper uses
triggered_at; queue latency is bounded by the 20s drain tick)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, ReviewRunItem, Workspace
from call_manager.proposal_assist.turns.caps import _stockholm_day_start_utc


def active_review_run(session: Session, *, workspace_id: uuid.UUID) -> ReviewRun | None:
    return session.scalar(
        select(ReviewRun).where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.status.in_(("queued", "running")),
        )
    )


def enqueue_review_run(
    session: Session, *, workspace_id: uuid.UUID, draft_sha: str
) -> ReviewRun:
    """Create a queued run, or return the existing active (queued|running) one.
    Race-safe against uq_review_runs_active via a SAVEPOINT (same pattern as
    enqueue_evaluator_run)."""
    existing = active_review_run(session, workspace_id=workspace_id)
    if existing is not None:
        return existing
    run = ReviewRun(workspace_id=workspace_id, status="queued", draft_sha=draft_sha)
    try:
        with session.begin_nested():
            session.add(run)
            session.flush()
    except IntegrityError:
        if run in session:
            session.expunge(run)
        winner = active_review_run(session, workspace_id=workspace_id)
        if winner is None:
            raise RuntimeError(
                "review enqueue lost the active-unique race but found no active run"
            ) from None
        return winner
    return run


def claim_next_review_run(session: Session) -> ReviewRun | None:
    """Claim the oldest queued run whose workspace is LIVE and has no running run.
    FOR UPDATE SKIP LOCKED. Soft-deleted workspaces excluded.

    The join captures lifecycle_generation IN THE SAME STATEMENT as the FOR UPDATE
    lock (Global Constraint 2). A separate SELECT would reopen the delete/restore
    race the fence exists to close.

    `of=ReviewRun` keeps the lock on the run row only. Without `of=`, PostgreSQL
    also locks the joined Workspace row, putting this claim in the delete path's way
    and inverting the lock order the architecture spec fixes in §5.

    Note: ReviewRun has no started_at column (the reaper uses triggered_at; queue
    latency is bounded by the 20s drain tick)."""
    running_ws = select(ReviewRun.workspace_id).where(ReviewRun.status == "running")
    row = session.execute(
        select(ReviewRun, Workspace.lifecycle_generation)
        .join(Workspace, Workspace.id == ReviewRun.workspace_id)
        .where(
            ReviewRun.status == "queued",
            ReviewRun.workspace_id.not_in(running_ws),
            Workspace.soft_deleted_at.is_(None),
        )
        .order_by(ReviewRun.triggered_at)
        .limit(1)
        # `of=` keeps FOR UPDATE on the RUN only. Without it PostgreSQL would
        # also lock the Workspace row, putting this claim in the delete path's
        # way and inverting the lock order the architecture spec fixes in §5.
        .with_for_update(of=ReviewRun, skip_locked=True)
    ).first()
    if row is None:
        return None
    run, generation = row
    run.status = "running"
    run.claimed_at_generation = generation
    session.flush()
    return run


def cancel_review_runs_and_items_for_workspace(
    session: Session, *, workspace_id: uuid.UUID
) -> int:
    """Cancel a workspace's active review runs AND their active items (soft-delete
    handler). ONE atomic UPDATE ... RETURNING per table — never load-then-write.

    Items are terminalized HERE, durably, because retirement cancellation does not
    reach a child's finalization session: CancelledError propagates out of the
    awaited turn, the `finally` runs the adapter terminate, and it keeps
    propagating — the finalization block AFTER the try never executes. Without
    this, a cancelled child leaves its ReviewRunItem in 'running' on a deleted
    workspace with nothing to repair it.

    LOCK ORDER: runs BEFORE items (normative)."""
    ids = (
        session.execute(
            update(ReviewRun)
            .where(
                ReviewRun.workspace_id == workspace_id,
                ReviewRun.status.in_(("queued", "running")),
            )
            .values(status="cancelled", completed_at=datetime.now(UTC))
            .returning(ReviewRun.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    if ids:
        session.execute(
            update(ReviewRunItem)
            .where(
                ReviewRunItem.review_run_id.in_(ids),
                ReviewRunItem.status.in_(("queued", "running")),
            )
            .values(
                status="cancelled",
                error_message="cancelled: workspace deleted",
                completed_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
    session.flush()
    return len(ids)


def rollup_run_totals(run: ReviewRun, items: Iterable[ReviewRunItem]) -> None:
    """Set a run's token/dollar totals from its items (idempotent; pure compute).

    Shared by finalize (pass_runner), the reaper, and the post-reaper late-write
    guard so the run-level totals — which ``review_dollars_today`` sums for the
    daily cap — reflect every item's recorded spend no matter which path finalizes
    the run. Keeping this in one place stops the three rollups from drifting."""
    items = list(items)
    run.input_tokens = sum((i.input_tokens or 0) for i in items)
    run.output_tokens = sum((i.output_tokens or 0) for i in items)
    dollars = sum((i.estimated_dollars or 0) for i in items)
    run.estimated_dollars = (dollars or None) and Decimal(dollars).quantize(
        Decimal("0.001")
    )


def reap_stuck_review_runs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running run whose triggered_at is older than
    the TTL (ReviewRun has no started_at — queue latency is bounded by the drain
    tick, so triggered_at ~= run start). Pure DB. FOR UPDATE SKIP LOCKED.

    Before flipping the run to ``failed`` it rolls the run's item token/dollar
    totals onto the run (``rollup_run_totals``), mirroring finalize. Finalize
    early-returns once the run is no longer ``running`` (the reaper got there
    first), so without this the run total stays null and ``review_dollars_today``
    — which sums run-level estimated_dollars — silently undercounts spend the
    items already incurred, letting the daily cap be exceeded. It also fails any
    still-queued/running item so the panel never shows stale rows under a failed
    run. SKIP LOCKED means an in-flight finalize holding the row lock is left to
    finish normally. A sub-call that records its tokens AFTER this rollup re-rolls
    the run total under the parent-row lock (see pass_runner._run_one_call)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(ReviewRun)
        .where(ReviewRun.status == "running", ReviewRun.triggered_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    now = datetime.now(UTC)
    for run in rows:
        items = session.scalars(
            select(ReviewRunItem).where(ReviewRunItem.review_run_id == run.id)
        ).all()
        rollup_run_totals(run, items)
        for it in items:
            if it.status in ("queued", "running"):
                it.status = "failed"
                it.error_message = (
                    (it.error_message or "") + " [reaper: run timed out]"
                ).strip()
                if it.completed_at is None:
                    it.completed_at = now
        run.status = "failed"
        run.completed_at = now
    session.flush()
    return len(rows)


def latest_review_run(session: Session, *, workspace_id: uuid.UUID) -> ReviewRun | None:
    return session.scalar(
        select(ReviewRun)
        .where(ReviewRun.workspace_id == workspace_id)
        .order_by(ReviewRun.triggered_at.desc())
        .limit(1)
    )


def review_runs_today(session: Session, *, workspace_id: uuid.UUID) -> int:
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(func.count())
        .select_from(ReviewRun)
        .where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.triggered_at >= day_start,
            ReviewRun.status != "cancelled",
        )
    )
    return int(total or 0)


def review_dollars_today(session: Session, *, workspace_id: uuid.UUID) -> Decimal:
    # B9 defect 5: INCLUDE cancelled runs in the dollar sum. _run_one_call
    # deliberately re-rolls a late sub-call's real spend onto a run that was
    # cancelled mid-flight (pass_runner.py), so a cancelled run CAN carry real
    # estimated_dollars. Excluding cancelled (as review_runs_today does for the
    # run *count*) silently undercounts the daily dollar budget. The run count
    # still excludes cancelled (a cancelled run didn't "use up" a run slot); only
    # the dollar accounting counts the spend that actually happened.
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(func.coalesce(func.sum(ReviewRun.estimated_dollars), 0)).where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.triggered_at >= day_start,
        )
    )
    return Decimal(total or 0)


def review_run_cap(ws: Workspace, settings: Settings) -> int:
    if ws is not None and ws.review_runs_per_day_override is not None:
        return ws.review_runs_per_day_override
    return settings.review_runs_per_day_default


def review_dollar_cap(ws: Workspace, settings: Settings) -> Decimal:
    if ws is not None and ws.review_dollar_cap_override is not None:
        return ws.review_dollar_cap_override
    return settings.review_dollar_cap_default


def estimate_dollars(tokens_in: int, tokens_out: int, settings: Settings) -> Decimal:
    """Conservative cost estimate. The adapter bundles base+cache_creation+cache_read
    into tokens_in (contract.py exposes no separate cache-read), so price the whole
    tokens_in at the input rate. Quantize to the item column scale (4 dp)."""
    cost = (
        Decimal(int(tokens_in or 0)) / Decimal(1_000_000) * settings.review_price_input_per_mtok
        + Decimal(int(tokens_out or 0)) / Decimal(1_000_000) * settings.review_price_output_per_mtok
    )
    return cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
