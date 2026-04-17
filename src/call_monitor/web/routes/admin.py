from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from call_monitor.auth.dependencies import require_admin
from call_monitor.db.models import (
    Call,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.ranking.backfill import backfill_profile

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
def admin_index(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        rows = session.execute(
            select(Source, func.count(Call.id).label("call_count"))
            .outerjoin(Call, Call.source_id == Source.id)
            .group_by(Source.id)
            .order_by(Source.name)
        ).all()
        sources = [
            {
                "name": src.name,
                "slug": src.slug,
                "last_fetched_at": src.last_fetched_at,
                "call_count": cc,
            }
            for src, cc in rows
        ]
        # Ranking-queue counts by status
        queue_rows = session.execute(
            select(RankingJob.status, func.count(RankingJob.id))
            .group_by(RankingJob.status)
        ).all()
        queue_counts = {status: count for status, count in queue_rows}

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "sources": sources,
            "queue_counts": queue_counts,
            "queue_status_pending": RankingJobStatus.PENDING.value,
            "queue_status_running": RankingJobStatus.RUNNING.value,
            "queue_status_failed": RankingJobStatus.FAILED.value,
            "queue_status_done": RankingJobStatus.DONE.value,
            "user": user,
        },
    )


@router.post("/admin/rerank")
def admin_rerank(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    """Enqueue ranking jobs for every active profile × every Call from
    the last 90 days. Idempotent (partial unique index dedup-guards).

    `backfill_profile` uses a single bulk INSERT per profile, so the
    total round-trip cost is O(active profiles), not O(active profiles
    × calls). Even with 15 profiles this stays well under any sane
    reverse-proxy timeout.
    """
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        active_profile_ids = session.execute(
            select(Profile.id).where(Profile.is_active.is_(True))
        ).scalars().all()
    for pid in active_profile_ids:
        backfill_profile(session_factory=session_factory, profile_id=pid)
    return RedirectResponse(url="/admin", status_code=303)
