from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from call_monitor.auth.dependencies import require_admin
from call_monitor.db.models import (
    Call,
    CallRanking,
    DigestRun,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from call_monitor.digest.send import week_start as _week_start
from call_monitor.ranking.backfill import backfill_profile

router = APIRouter()


def _digest_panel_context(session) -> dict:
    now = datetime.now(UTC)
    week_start_dt = _week_start(now)

    last_run = session.execute(
        select(DigestRun).order_by(DigestRun.sent_at.desc()).limit(1)
    ).scalar_one_or_none()
    runs_this_week = session.execute(
        select(func.count(DigestRun.id)).where(DigestRun.sent_at >= week_start_dt)
    ).scalar_one()
    disabled_users = session.execute(
        select(func.count(User.id)).where(User.digest_disabled_at.is_not(None))
    ).scalar_one()
    return {
        "digest_last_sent_at": last_run.sent_at if last_run else None,
        "digest_runs_this_week": runs_this_week,
        "digest_disabled_users": disabled_users,
    }


def _v2_queue_depth(session) -> dict[str, int]:
    """Count active (pending + running) ranking jobs grouped by scheme.

    Used by /admin to surface dual-scheme rollout health: operators need to
    see at a glance whether the v2 queue is backing up relative to v1.
    """
    rows = session.execute(
        select(RankingJob.scheme, func.count(RankingJob.id))
        .where(RankingJob.status.in_([RankingJobStatus.PENDING, RankingJobStatus.RUNNING]))
        .group_by(RankingJob.scheme)
    ).all()
    return dict(rows)


def _per_profile_scheme(session) -> list[dict]:
    """One row per active profile: id, name, ranking_scheme, last ranked_at.

    `last_ranked` is the most recent ranking timestamp for the profile's
    *active* scheme — joining on scheme avoids the misleading "v1 last
    ranked yesterday" reading on a profile that flipped to v2 today.
    """
    rows = session.execute(
        select(
            Profile.id, Profile.name, Profile.ranking_scheme,
            func.max(CallRanking.ranked_at).label("last_ranked"),
        )
        .outerjoin(
            CallRanking,
            (CallRanking.profile_id == Profile.id)
            & (CallRanking.scheme == Profile.ranking_scheme),
        )
        .where(Profile.is_active.is_(True))
        .group_by(Profile.id, Profile.name, Profile.ranking_scheme)
        .order_by(Profile.name)
    ).all()
    return [
        {"id": pid, "name": name, "scheme": scheme, "last_ranked": last}
        for pid, name, scheme, last in rows
    ]


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
        context = {
            "sources": sources,
            "queue_counts": queue_counts,
            "queue_status_pending": RankingJobStatus.PENDING.value,
            "queue_status_running": RankingJobStatus.RUNNING.value,
            "queue_status_failed": RankingJobStatus.FAILED.value,
            "queue_status_done": RankingJobStatus.DONE.value,
            "user": user,
        }
        context.update(_digest_panel_context(session))
        context["v2_queue_depth"] = _v2_queue_depth(session)
        context["per_profile_scheme"] = _per_profile_scheme(session)

    return request.app.state.templates.TemplateResponse(
        request=request,
        name="admin.html",
        context=context,
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
