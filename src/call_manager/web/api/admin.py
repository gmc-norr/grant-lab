from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_, select, text

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
from call_manager.ranking.backfill import BACKFILL_DAYS, backfill_profile
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
    non_blank_call = func.length(func.trim(Call.title)) > 0
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


class SourceOut(BaseModel):
    name: str
    slug: str
    last_fetched_at: datetime | None
    call_count: int


class QueueCountsOut(BaseModel):
    pending: int
    running: int
    done: int
    failed: int


class DisabledUserOut(BaseModel):
    id: int
    email: str
    disabled_at: datetime


class DigestPanelOut(BaseModel):
    last_sent_at: datetime | None
    runs_this_week: int
    disabled_users: int
    disabled_user_rows: list[DisabledUserOut]


class EmbeddingCoverageOut(BaseModel):
    call_embedded: int
    call_total: int
    call_stale: int
    profile_embedded: int
    profile_total: int
    profile_stale: int
    current_model: str


class GoldenSetReportOut(BaseModel):
    n_fixture: int
    n_compared: int
    tau: float | None


class AdminDashboardOut(BaseModel):
    sources: list[SourceOut]
    queue_counts: QueueCountsOut
    rerank_estimate: int
    active_profiles_count: int
    recent_calls_count: int
    backfill_days: int
    digest: DigestPanelOut
    embedding_coverage: EmbeddingCoverageOut
    golden_set_report: GoldenSetReportOut | None
    calibration_warning: str | None


@router.get("/admin/dashboard", response_model=AdminDashboardOut)
def admin_dashboard(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        rows = session.execute(
            select(Source, func.count(Call.id).label("call_count"))
            .outerjoin(Call, Call.source_id == Source.id)
            .group_by(Source.id)
            .order_by(Source.name)
        ).all()
        sources = [
            SourceOut(
                name=src.name, slug=src.slug, last_fetched_at=src.last_fetched_at, call_count=cc
            )
            for src, cc in rows
        ]
        queue_rows = session.execute(
            select(RankingJob.status, func.count(RankingJob.id)).group_by(RankingJob.status)
        ).all()
        counts = {status: count for status, count in queue_rows}
        queue_counts = QueueCountsOut(
            pending=counts.get(RankingJobStatus.PENDING.value, 0),
            running=counts.get(RankingJobStatus.RUNNING.value, 0),
            done=counts.get(RankingJobStatus.DONE.value, 0),
            failed=counts.get(RankingJobStatus.FAILED.value, 0),
        )
        active_profiles_count = session.execute(
            select(func.count(Profile.id)).where(Profile.is_active.is_(True))
        ).scalar_one()
        recent_cutoff = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)
        recent_calls_count = session.execute(
            select(func.count(Call.id)).where(Call.published_at >= recent_cutoff)
        ).scalar_one()

        digest_ctx = _digest_panel_context(session)
        coverage_ctx = _embedding_coverage_context(session, current_model=settings.embedding_model)
        digest = DigestPanelOut(
            last_sent_at=digest_ctx["digest_last_sent_at"],
            runs_this_week=digest_ctx["digest_runs_this_week"],
            disabled_users=digest_ctx["digest_disabled_users"],
            disabled_user_rows=[
                DisabledUserOut(id=r["id"], email=r["email"], disabled_at=r["digest_disabled_at"])
                for r in digest_ctx["digest_disabled_user_rows"]
            ],
        )
        coverage = EmbeddingCoverageOut(**coverage_ctx["embedding_coverage"])

        # Lazy import: keeps the /api module cheap and dodges any ranking->web cycle.
        from call_manager.ranking.eval import load_last_report

        report = load_last_report()
        golden = (
            GoldenSetReportOut(
                n_fixture=report.n_fixture, n_compared=report.n_compared, tau=report.tau
            )
            if report is not None
            else None
        )
        calibration_warning = getattr(request.app.state, "embedding_calibration_warning", None)

    return AdminDashboardOut(
        sources=sources,
        queue_counts=queue_counts,
        rerank_estimate=active_profiles_count * recent_calls_count,
        active_profiles_count=active_profiles_count,
        recent_calls_count=recent_calls_count,
        backfill_days=BACKFILL_DAYS,
        digest=digest,
        embedding_coverage=coverage,
        golden_set_report=golden,
        calibration_warning=calibration_warning,
    )


def _drafting_panel_context(session, settings) -> dict:
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


class GlobalCapOut(BaseModel):
    allowed: bool
    used_in: int
    used_out: int
    cap_in: int
    cap_out: int
    reason: str | None


class PerWorkspaceOut(BaseModel):
    id: uuid.UUID
    title: str
    owner_email: str
    used_in: int
    used_out: int


class RecentEventOut(BaseModel):
    created_at: datetime
    workspace_id: uuid.UUID
    kind: str
    payload: dict


class AdminDraftingOut(BaseModel):
    drafting_enabled: bool
    global_cap: GlobalCapOut
    per_workspace: list[PerWorkspaceOut]
    recent_events: list[RecentEventOut]


@router.get("/admin/drafting", response_model=AdminDraftingOut)
def admin_drafting(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        ctx = _drafting_panel_context(session, settings)
        cap = ctx["global_cap"]
        per_workspace = [
            PerWorkspaceOut(
                id=row.id,
                title=row.title,
                owner_email=row.owner_email,
                used_in=row.used_in,
                used_out=row.used_out,
            )
            for row in ctx["per_workspace"]
        ]
        recent_events = [
            RecentEventOut(
                created_at=e.created_at, workspace_id=e.workspace_id, kind=e.kind, payload=e.payload
            )
            for e in ctx["recent_events"]
        ]
        result = AdminDraftingOut(
            drafting_enabled=ctx["drafting_enabled"],
            global_cap=GlobalCapOut(
                allowed=cap.allowed,
                used_in=cap.used_in,
                used_out=cap.used_out,
                cap_in=cap.cap_in,
                cap_out=cap.cap_out,
                reason=cap.reason,
            ),
            per_workspace=per_workspace,
            recent_events=recent_events,
        )
    return result


class RerankOut(BaseModel):
    enqueued: int


class TriggeredOut(BaseModel):
    triggered: bool


class ReenableIn(BaseModel):
    user_id: int


class AdminActionOut(BaseModel):
    id: int


def _scheduler(request: Request):
    """The app's APScheduler, or None if absent (e.g. before lifespan)."""
    return getattr(request.app.state, "scheduler", None)


@router.post("/admin/rerank", response_model=RerankOut)
def admin_rerank(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        active_profile_ids = (
            session.execute(select(Profile.id).where(Profile.is_active.is_(True))).scalars().all()
        )
    for pid in active_profile_ids:
        backfill_profile(session_factory=session_factory, profile_id=pid)
    return RerankOut(enqueued=len(active_profile_ids))


@router.post("/admin/digest/send", response_model=TriggeredOut)
def admin_send_digest(
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    return TriggeredOut(triggered=trigger_job_now(_scheduler(request), "digest:weekly"))


@router.post("/admin/digest/reenable", response_model=AdminActionOut)
def admin_reenable_user(
    request: Request,
    body: ReenableIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as session:
        target = session.get(User, body.user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="unknown user")
        target.digest_disabled_at = None
        session.commit()
    return AdminActionOut(id=body.user_id)


@router.post("/admin/sources/{slug}/fetch", response_model=TriggeredOut)
def admin_fetch_source(
    slug: str,
    request: Request,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as session:
        src = session.execute(select(Source).where(Source.slug == slug)).scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=404, detail="unknown source")
    return TriggeredOut(triggered=trigger_job_now(_scheduler(request), f"ingest:{slug}"))
