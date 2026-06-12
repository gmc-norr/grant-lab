from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import (
    Call,
    ChatTurn,
    DigestRun,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
    Workspace,
    WorkspaceEvent,
)
from call_manager.digest.send import week_start as _week_start
from call_manager.ranking.backfill import backfill_profile
from call_manager.scheduler import trigger_job_now

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
    disabled_rows = session.execute(
        select(User.id, User.email, User.digest_disabled_at)
        .where(User.digest_disabled_at.is_not(None))
        .order_by(User.digest_disabled_at.desc())
    ).all()
    return {
        "digest_last_sent_at": last_run.sent_at if last_run else None,
        "digest_runs_this_week": runs_this_week,
        "digest_disabled_users": disabled_users,
        "digest_disabled_user_rows": [
            {"id": r.id, "email": r.email, "digest_disabled_at": r.digest_disabled_at}
            for r in disabled_rows
        ],
    }


def _embedding_coverage_context(session, *, current_model: str) -> dict:
    """Counts of Calls/Profiles missing embeddings or embedded against an
    older model. Lets ops see at a glance when the embedding pre-gate is
    silently degrading to UNKNOWN (which masks topic-fit signal).

    Mirrors the refresh-missing tick's blank-text filter. The tick (see
    refresh_missing_embeddings_tick) explicitly excludes rows with empty
    title/description because there's nothing to embed — counting them
    here would surface permanent false-stale and train operators to
    ignore the badge.
    """
    non_blank_call = func.length(func.trim(Call.title)) > 0
    # "Embeddable" mirrors build_profile_text: description non-blank, OR areas
    # non-empty, OR org capabilities/patient_populations non-empty. MUST stay in
    # lockstep with embeddings/similarity.build_profile_text (see Phase 5).
    non_blank_profile = or_(
        func.length(func.trim(func.coalesce(Profile.description, ""))) > 0,
        func.jsonb_array_length(func.coalesce(Profile.structured["areas"], text("'[]'::jsonb")))
        > 0,
        func.jsonb_array_length(
            func.coalesce(Profile.structured["org"]["capabilities"], text("'[]'::jsonb"))
        )
        > 0,
        func.jsonb_array_length(
            func.coalesce(Profile.structured["org"]["patient_populations"], text("'[]'::jsonb"))
        )
        > 0,
    )
    call_stale = session.execute(
        select(func.count(Call.id)).where(
            non_blank_call,
            or_(Call.embedding.is_(None), Call.embedding_model != current_model),
        )
    ).scalar_one()
    call_total = session.execute(select(func.count(Call.id)).where(non_blank_call)).scalar_one()
    profile_stale = session.execute(
        select(func.count(Profile.id)).where(
            non_blank_profile,
            or_(Profile.embedding.is_(None), Profile.embedding_model != current_model),
        )
    ).scalar_one()
    profile_total = session.execute(
        select(func.count(Profile.id)).where(non_blank_profile)
    ).scalar_one()
    return {
        "embedding_coverage": {
            "call_stale": call_stale,
            "call_total": call_total,
            "call_embedded": call_total - call_stale,
            "profile_stale": profile_stale,
            "profile_total": profile_total,
            "profile_embedded": profile_total - profile_stale,
            "current_model": current_model,
        }
    }


def _drafting_panel_context(session: Session, settings) -> dict:
    """Aggregate everything the /admin/drafting page renders:
    - kill switch state (read from settings)
    - global cap usage today (input + output sums + caps)
    - per-workspace usage today (id, title, owner_email, used_in, used_out)
    - last 50 failed/cancelled cloud_turn events across workspaces
    """
    from call_manager.proposal_assist.turns.caps import (
        _stockholm_day_start_utc,
        check_global_daily_token_cap,
    )

    global_cap = check_global_daily_token_cap(session, settings=settings)

    day_start_utc = _stockholm_day_start_utc()
    per_workspace_rows = list(
        session.execute(
            select(
                Workspace.id,
                Workspace.title,
                User.email.label("owner_email"),
                func.coalesce(func.sum(ChatTurn.tokens_in), 0).label("used_in"),
                func.coalesce(func.sum(ChatTurn.tokens_out), 0).label("used_out"),
            )
            .join(User, User.id == Workspace.owner_id)
            .outerjoin(
                ChatTurn,
                (ChatTurn.workspace_id == Workspace.id) & (ChatTurn.created_at >= day_start_utc),
            )
            .where(Workspace.soft_deleted_at.is_(None))
            .group_by(Workspace.id, Workspace.title, User.email)
            .order_by(func.coalesce(func.sum(ChatTurn.tokens_in), 0).desc())
        ).all()
    )

    recent_events = list(
        session.scalars(
            select(WorkspaceEvent)
            .where(WorkspaceEvent.kind.in_(["cloud_turn_failed", "cloud_turn_cancelled"]))
            .order_by(WorkspaceEvent.created_at.desc())
            .limit(50)
        ).all()
    )

    return {
        "drafting_enabled": settings.drafting_enabled,
        "global_cap": global_cap,
        "per_workspace": per_workspace_rows,
        "recent_events": recent_events,
    }


@router.get("/admin/drafting", response_class=HTMLResponse)
def admin_drafting_panel(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
) -> HTMLResponse:
    """Read-only admin panel for the proposal-assist drafting subsystem.

    Toggling the kill switch / overriding the global cap from this UI
    is intentionally NOT implemented in v1 (requires app restart). The
    panel surfaces state so an operator can decide whether to restart
    with a new env.
    """
    session_factory = request.app.state.session_factory
    settings = request.app.state.settings
    with session_factory() as session:
        context = _drafting_panel_context(session, settings)
    context["request"] = request
    context["user"] = user
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="admin_drafting.html",
        context=context,
    )


@router.get("/admin", response_class=HTMLResponse)
def admin_index(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    settings = request.app.state.settings
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
            select(RankingJob.status, func.count(RankingJob.id)).group_by(RankingJob.status)
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
        context.update(_embedding_coverage_context(session, current_model=settings.embedding_model))
        # Lazy import: keeps admin route module-load cheap and dodges any
        # potential cycle via ranking → db → web → admin.
        from call_manager.ranking.eval import load_last_report

        context["golden_set_report"] = load_last_report()
        context["embedding_calibration_warning"] = getattr(
            request.app.state, "embedding_calibration_warning", None
        )

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
        active_profile_ids = (
            session.execute(select(Profile.id).where(Profile.is_active.is_(True))).scalars().all()
        )
    for pid in active_profile_ids:
        backfill_profile(session_factory=session_factory, profile_id=pid)
    return RedirectResponse(url="/admin?ok=rerank", status_code=303)


@router.post("/admin/digest/reenable")
def admin_reenable_user(
    request: Request,
    user_id: int = Form(...),
    user: User = Depends(require_admin),  # noqa: B008
):
    """Clear digest_disabled_at for a bounced user so the weekly digest
    resumes for them. Idempotent: a no-op if already enabled."""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        target = session.get(User, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="unknown user")
        target.digest_disabled_at = None
        session.commit()
    return RedirectResponse(url="/admin?ok=reenable", status_code=303)


def _scheduler(request: Request):
    """The app's APScheduler, or None if absent (e.g. before lifespan)."""
    return getattr(request.app.state, "scheduler", None)


@router.post("/admin/sources/{slug}/fetch")
def admin_fetch_source(
    slug: str,
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    """Trigger the per-source ingest job (id=ingest:{slug}) to run now."""
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        src = session.execute(select(Source).where(Source.slug == slug)).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="unknown source")
    ok = trigger_job_now(_scheduler(request), f"ingest:{slug}")
    key = "ok=fetch" if ok else "err=fetch"
    return RedirectResponse(url=f"/admin?{key}", status_code=303)


@router.post("/admin/digest/send")
def admin_send_digest(
    request: Request,
    user: User = Depends(require_admin),  # noqa: B008
):
    """Trigger the weekly digest job to run now. The job is absent when no
    email backend is configured; run_digest is week-idempotent, so a manual
    run during the same week safely de-dupes via the DigestRun claim."""
    ok = trigger_job_now(_scheduler(request), "digest:weekly")
    key = "ok=digest" if ok else "err=digest_unconfigured"
    return RedirectResponse(url=f"/admin?{key}", status_code=303)
