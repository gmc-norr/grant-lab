"""Pure-DB helpers for the review_runs queue + the daily caps + token→dollar pricing.
The caller owns the transaction (these flush, never commit), mirroring
evaluator/jobs.py. Bespoke: no started_at column on ReviewRun (the reaper uses
triggered_at; queue latency is bounded by the 20s drain tick)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, Workspace
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
    FOR UPDATE SKIP LOCKED. Soft-deleted workspaces excluded."""
    running_ws = select(ReviewRun.workspace_id).where(ReviewRun.status == "running")
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    run = session.scalar(
        select(ReviewRun)
        .where(
            ReviewRun.status == "queued",
            ReviewRun.workspace_id.not_in(running_ws),
            ReviewRun.workspace_id.in_(live_ws),
        )
        .order_by(ReviewRun.triggered_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if run is None:
        return None
    run.status = "running"
    session.flush()
    return run


def cancel_review_runs_for_workspace(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Cancel any active (queued|running) review run for a workspace (soft-delete
    handler). ONE atomic UPDATE ... RETURNING — never an ORM load-then-write."""
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
    session.flush()
    return len(ids)


def reap_stuck_review_runs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running run whose triggered_at is older than
    the TTL (ReviewRun has no started_at — queue latency is bounded by the drain
    tick, so triggered_at ~= run start). Pure DB. FOR UPDATE SKIP LOCKED."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(ReviewRun)
        .where(ReviewRun.status == "running", ReviewRun.triggered_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for run in rows:
        run.status = "failed"
        run.completed_at = datetime.now(UTC)
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
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(func.coalesce(func.sum(ReviewRun.estimated_dollars), 0)).where(
            ReviewRun.workspace_id == workspace_id,
            ReviewRun.triggered_at >= day_start,
            ReviewRun.status != "cancelled",
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
