from __future__ import annotations

import logging
from typing import Protocol

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import sessionmaker

from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
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
    vinnova_api_key: str | None,
    alerter: ConsecutiveFailureTracker | _Alerter,
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

    if vinnova_api_key:
        def _vinnova_job() -> None:
            adapter = VinnovaAdapter(api_key=vinnova_api_key, base_url=vinnova_base_url)
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
    else:
        _logger.warning(
            "scheduler.vinnova_skipped",
            extra={"event": "vinnova_skipped", "reason": "VINNOVA_API_KEY not set"},
        )

    if start:
        sched.start()
    return sched
