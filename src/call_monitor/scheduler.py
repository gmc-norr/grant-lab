from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime as _dt
from typing import Protocol

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import sessionmaker

from call_monitor.digest.send import run_digest
from call_monitor.email.client import EmailClient
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.setwise import run_setwise_for_profile
from call_monitor.ranking.worker import drain_queue
from call_monitor.sources.eu_portal import EUPortalAdapter
from call_monitor.sources.vinnova import VinnovaAdapter

_logger = logging.getLogger(__name__)


class _SafeBackgroundScheduler(BackgroundScheduler):
    """BackgroundScheduler that silently ignores shutdown() when not running.

    APScheduler raises SchedulerNotRunningError when shutdown() is called on
    a scheduler that was never started (e.g. build_scheduler(..., start=False)
    in tests). This subclass makes shutdown() a no-op in that state so callers
    don't need to guard it.
    """

    def shutdown(self, wait: bool = True) -> None:
        if self.state == STATE_STOPPED:
            return
        super().shutdown(wait=wait)


class _Alerter(Protocol):
    def record_failure(self, source_slug: str, *, error: str | None = None) -> None: ...
    def record_success(self, source_slug: str) -> None: ...


def build_scheduler(
    *,
    session_factory: sessionmaker,
    eu_portal_base_url: str,
    vinnova_base_url: str,
    alerter: ConsecutiveFailureTracker | _Alerter,
    ollama_url: str,
    gemma_model: str,
    ranking_tick_seconds: int,
    ranking_queue_batch: int,
    digest_email_client: EmailClient | None = None,
    digest_settings=None,  # duck-typed: needs .digest_dry_run, .admin_dashboard_url
    start: bool = True,
) -> BackgroundScheduler:
    """Build (and optionally start) the daily ingestion scheduler.

    Single-instance constraint: this is APScheduler in-process. Two app
    replicas would each run their own scheduler — duplicate fetches and
    duplicate ranking jobs once Plan 3 lands. Deployment must enforce one
    replica (see CLAUDE.md / spec).

    Multi-instance escape hatch (documented in design spec, "Ingestion ▸
    Multi-instance future"): wrap each scheduled job body in a Postgres
    `pg_try_advisory_lock(hashtext('ingest:' || source_slug))`, returning
    early if the lock isn't acquired. That lets multiple replicas coexist
    while guaranteeing only one runs each job. Out of scope for v1 —
    flagged here so a future replica-bump deliberately revisits this.
    """
    sched = _SafeBackgroundScheduler(timezone="Europe/Stockholm")

    def _eu_job() -> None:
        adapter = EUPortalAdapter(base_url=eu_portal_base_url)
        run_ingestion(session_factory=session_factory, adapter=adapter, alerter=alerter)

    sched.add_job(
        _eu_job,
        trigger=CronTrigger(hour=3, minute=0),
        id="ingest:eu_portal",
        name="Ingest EU Funding & Tenders Portal",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    def _vinnova_job() -> None:
        adapter = VinnovaAdapter(base_url=vinnova_base_url)
        run_ingestion(
            session_factory=session_factory, adapter=adapter, alerter=alerter
        )

    sched.add_job(
        _vinnova_job,
        trigger=CronTrigger(hour=3, minute=15),
        id="ingest:vinnova",
        name="Ingest Vinnova",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    ollama = OllamaClient(base_url=ollama_url, model=gemma_model)

    def _rank_drain_job() -> None:
        drain_queue(
            session_factory=session_factory,
            client=ollama,
            batch_size=ranking_queue_batch,
        )

    sched.add_job(
        _rank_drain_job,
        trigger=IntervalTrigger(seconds=ranking_tick_seconds),
        id="rank:drain",
        name="Drain ranking job queue",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    def _setwise_tick() -> None:
        from sqlalchemy import select

        from call_monitor.db.models import Profile

        with session_factory() as session:
            v2_profile_ids = session.execute(
                select(Profile.id).where(
                    Profile.is_active.is_(True),
                    Profile.ranking_scheme == "v2",
                )
            ).scalars().all()

        now = _dt.now(UTC)
        for pid in v2_profile_ids:
            try:
                run_setwise_for_profile(
                    session_factory=session_factory,
                    client=ollama,
                    profile_id=pid,
                    now=now,
                )
            except Exception as e:  # noqa: BLE001
                _logger.exception(
                    "ranking.setwise.profile_failed",
                    extra={
                        "event": "setwise_profile_failed",
                        "profile_id": pid,
                        "error": str(e),
                    },
                )

    sched.add_job(
        _setwise_tick,
        trigger=CronTrigger(hour=4, minute=0),
        id="setwise:daily",
        name="Daily setwise borderline pass",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    def _golden_tick() -> None:
        # Lazy import: Task 6 ships call_monitor.ranking.eval after this task.
        # Importing inside the job body lets Task 4 land first; the import
        # only fires when the scheduled job actually runs.
        from call_monitor.ranking.eval import run_golden_set_eval

        try:
            run_golden_set_eval(session_factory=session_factory)
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "ranking.golden_eval.failed",
                extra={"event": "golden_eval_failed", "error": str(e)},
            )

    sched.add_job(
        _golden_tick,
        trigger=CronTrigger(day_of_week="mon", hour=6, minute=0),
        id="golden_eval:weekly",
        name="Weekly golden-set evaluation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    if digest_email_client is not None and digest_settings is not None:
        def _digest_tick() -> None:
            now = _dt.now(UTC)
            _logger.info("digest.tick.start", extra={"event": "digest_tick_start"})
            try:
                summary = run_digest(
                    session_factory=session_factory,
                    email_client=digest_email_client,
                    settings=digest_settings,
                    now=now,
                )
                _logger.info(
                    "digest.tick.done",
                    extra={
                        "event": "digest_tick_done",
                        "users_processed": summary.users_processed,
                        "emails_sent": summary.emails_sent,
                        "bounces": summary.bounces,
                        "transport_errors": summary.transport_errors,
                        "users_disabled": summary.users_disabled,
                    },
                )
            except Exception as e:  # noqa: BLE001
                _logger.exception(
                    "digest.tick.error",
                    extra={"event": "digest_tick_error", "error": str(e)},
                )

        sched.add_job(
            _digest_tick,
            # Scheduler timezone is already Europe/Stockholm (see _SafeBackgroundScheduler
            # construction at top of build_scheduler), so day_of_week='mon' hour=7
            # fires at Stockholm Monday 07:00.
            trigger=CronTrigger(day_of_week="mon", hour=7, minute=0),
            id="digest:weekly",
            name="Weekly digest",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        _logger.info(
            "scheduler.digest_skipped",
            extra={
                "event": "digest_skipped",
                "reason": "no_email_config",
            },
        )

    if start:
        sched.start()
    return sched
