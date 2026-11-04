from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text

from grant_lab.auth.dependencies import require_admin
from grant_lab.db.models import (
    Call,
    DigestRun,
    Profile,
    RankingJob,
    RankingJobStatus,
    Source,
    User,
)
from grant_lab.digest.send import week_start as _week_start
from grant_lab.ranking.backfill import backfill_profile
from grant_lab.scheduler import trigger_job_now

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


SourceStatus = Literal["ok", "failing", "never_fetched", "stale"]


class SourceOut(BaseModel):
    name: str
    slug: str
    last_fetched_at: datetime | None
    call_count: int
    # The persisted failure streak, reset to 0 by the next successful fetch.
    # Shown because email alerts need ADMIN_ALERT_EMAIL and a transport, and
    # without them an operator could only infer a dead source from an old
    # timestamp (beta review 2026-10-31 F8; open since review 2026-10-06 M4).
    consecutive_failures: int
    status: SourceStatus = Field(
        description=(
            "failing: the latest fetch failed (consecutive_failures > 0); "
            "never_fetched: no successful fetch on record; stale: no success for "
            "longer than STALE_SOURCE_ALERT_DAYS; ok: otherwise. First match wins."
        )
    )


def source_status(src, *, now: datetime, stale_days: int) -> str:
    """The admin table's one-word verdict for a source, in precedence order."""
    if (src.consecutive_failures or 0) > 0:
        return "failing"
    if src.last_fetched_at is None:
        return "never_fetched"
    if src.last_fetched_at < now - timedelta(days=stale_days):
        return "stale"
    return "ok"


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
    open_calls_count: int
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
        now = datetime.now(UTC)
        sources = [
            SourceOut(
                name=src.name,
                slug=src.slug,
                last_fetched_at=src.last_fetched_at,
                call_count=cc,
                consecutive_failures=src.consecutive_failures or 0,
                status=source_status(src, now=now, stale_days=settings.stale_source_alert_days),
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
        open_calls_count = session.execute(
            select(func.count(Call.id)).where(
                or_(Call.deadline.is_(None), Call.deadline > func.now())
            )
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
        from grant_lab.ranking.eval import load_last_report

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
        rerank_estimate=active_profiles_count * open_calls_count,
        active_profiles_count=active_profiles_count,
        open_calls_count=open_calls_count,
        digest=digest,
        embedding_coverage=coverage,
        golden_set_report=golden,
        calibration_warning=calibration_warning,
    )


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
