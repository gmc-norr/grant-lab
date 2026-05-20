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

from call_monitor.auth.rate_limit import SlidingWindowLimiter
from call_monitor.config import Settings
from call_monitor.digest.send import run_digest
from call_monitor.email.client import EmailClient
from call_monitor.embeddings.client import OllamaEmbedClient
from call_monitor.embeddings.refresh import refresh_missing_embeddings_tick
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker
from call_monitor.ingestion.orchestrator import run_ingestion
from call_monitor.proposal_assist.drafting.controller import WorkspaceController
from call_monitor.proposal_assist.phases.setup import run_phase0_tick
from call_monitor.ranking.anthropic_client import AnthropicClient
from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.queue import reconcile_missing_rankings
from call_monitor.ranking.worker import drain_queue
from call_monitor.sources.registry import SOURCES

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
    settings: Settings,
    alerter: ConsecutiveFailureTracker | _Alerter,
    ollama_url: str,
    gemma_model: str,
    ranking_tick_seconds: int,
    ranking_queue_batch: int,
    embedding_pregate_threshold: float,
    embedding_hard_fail_threshold: float | None = None,
    anthropic_api_key: str | None = None,
    remote_rank_model: str = "claude-sonnet-4-6",
    remote_rank_cosine_threshold: float = 0.50,
    embed_client: OllamaEmbedClient,
    digest_email_client: EmailClient | None = None,
    digest_settings=None,  # duck-typed: needs .digest_dry_run, .admin_dashboard_url
    limiters: list[SlidingWindowLimiter] | None = None,
    workspace_controller: WorkspaceController,
    app_loop=None,  # asyncio.AbstractEventLoop | None
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

    for cfg in SOURCES:
        # Capture cfg by default-arg to avoid late-binding in the closure.
        # Without `_cfg=cfg`, every job's closure would resolve `cfg` at
        # call time and find the LAST value the loop variable held — i.e.
        # all jobs would run the last source. Default args are bound at
        # def-time, so each closure gets its own snapshot.
        def _ingest_job(_cfg=cfg) -> None:
            adapter = _cfg.build(settings)
            run_ingestion(
                session_factory=session_factory,
                adapter=adapter,
                alerter=alerter,
                embed_client=embed_client,
            )

        sched.add_job(
            _ingest_job,
            trigger=CronTrigger(hour=cfg.cron_hour, minute=cfg.cron_minute),
            id=f"ingest:{cfg.slug}",
            name=cfg.cron_name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    ollama = OllamaClient(base_url=ollama_url, model=gemma_model)
    # Build the remote rank client once if an API key is configured.
    # None ⇒ rank_pair never escalates regardless of cosine — the safe
    # default for environments without billing or network access to
    # Anthropic. See ranking/anthropic_client.py for the routing
    # semantics inside rank_pair.
    anthropic_client: AnthropicClient | None = None
    if anthropic_api_key:
        anthropic_client = AnthropicClient(api_key=anthropic_api_key, model=remote_rank_model)

    def _rank_drain_job() -> None:
        drain_queue(
            session_factory=session_factory,
            client=ollama,
            embedding_threshold=embedding_pregate_threshold,
            embedding_hard_fail_threshold=embedding_hard_fail_threshold,
            remote_client=anthropic_client,
            remote_cosine_threshold=remote_rank_cosine_threshold,
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

    def _golden_tick() -> None:
        # Lazy import: keeps scheduler module-load cheap and dodges any
        # potential cycle via ranking → db → web. The import only fires
        # when the scheduled job actually runs (weekly).
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

    def _embeddings_tick() -> None:
        try:
            refresh_missing_embeddings_tick(
                session_factory=session_factory,
                embed_client=embed_client,
                batch_size=200,
            )
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "embeddings.refresh_missing_tick.failed",
                extra={"event": "embedding_refresh_tick_failed", "error": str(e)},
            )
            return
        # No need to re-log summary — refresh_missing_embeddings_tick already does.

    sched.add_job(
        _embeddings_tick,
        trigger=CronTrigger(hour=3, minute=30),
        id="embeddings:refresh-missing",
        name="Daily backfill of missing embeddings",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    def _reconcile_rankings_tick() -> None:
        # Closes the orchestrator's step-1-succeeded-but-step-3-failed gap
        # (see ingestion/orchestrator.py atomicity caveats). A mid-flight
        # crash leaves calls ingested but no ranking jobs queued; this
        # daily sweep enqueues any (recent call × active profile) pair
        # that has no CallRanking and no active RankingJob.
        try:
            with session_factory() as session:
                n = reconcile_missing_rankings(session)
                session.commit()
                if n:
                    _logger.info(
                        "ranking.reconcile.tick_done",
                        extra={
                            "event": "ranking_reconcile_tick_done",
                            "enqueued": n,
                        },
                    )
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "ranking.reconcile.failed",
                extra={"event": "ranking_reconcile_failed", "error": str(e)},
            )

    sched.add_job(
        _reconcile_rankings_tick,
        # Run after the nightly ingest (Vinnova+EU are typically 2/3 AM)
        # and embedding refresh (3:30) so we catch any gaps those left
        # behind on the same day rather than a day late.
        trigger=CronTrigger(hour=4, minute=0),
        id="ranking:reconcile",
        name="Daily reconcile of orphaned ranking pairs",
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

    if limiters:

        def _rate_limit_sweep(_ls=tuple(limiters)) -> None:
            # Each limiter's dict of per-key deques grows with unique
            # (email|ip) attempts. Sweep reclaims any key whose entire
            # window has expired so memory stays bounded in a long-running
            # process.
            for lim in _ls:
                try:
                    removed = lim.sweep()
                    if removed:
                        _logger.info(
                            "rate_limit.sweep",
                            extra={
                                "event": "rate_limit_sweep",
                                "limiter": lim.name,
                                "removed": removed,
                            },
                        )
                except Exception as e:  # noqa: BLE001
                    _logger.exception(
                        "rate_limit.sweep.failed",
                        extra={
                            "event": "rate_limit_sweep_failed",
                            "limiter": lim.name,
                            "error": str(e),
                        },
                    )

        sched.add_job(
            _rate_limit_sweep,
            # Hourly is plenty — the windows are minutes, so stale keys
            # don't accumulate fast. Running more often just wastes cycles.
            trigger=IntervalTrigger(hours=1),
            id="rate_limit:sweep",
            name="Sweep expired rate-limit keys",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    # Phase 0 (proposal-assist) drain tick.
    def _phase0_tick() -> None:
        """Closure: APScheduler calls this with no args. We capture the
        settings + session_factory from build_scheduler's scope, and lazily
        construct an AnthropicClient per tick so a missing API key only
        skips work (rather than crashing scheduler startup)."""
        if not settings.anthropic_api_key:
            _logger.info("phase0 tick skipped: ANTHROPIC_API_KEY not set")
            return

        def make_client() -> AnthropicClient:
            return AnthropicClient(
                api_key=settings.anthropic_api_key,
                model=settings.proposal_assist_llm_model,
            )

        run_phase0_tick(
            session_factory=session_factory,
            client_factory=make_client,
            extractor_version=settings.extractor_version,
            rubric_extractor_version=settings.rubric_extractor_version,
            workspaces_dir=settings.workspaces_dir,
        )

    sched.add_job(
        _phase0_tick,
        trigger=IntervalTrigger(seconds=settings.phase0_tick_seconds),
        id="proposal_assist_phase0_tick",
        max_instances=1,
        coalesce=True,
    )

    # Drafting subprocess idle sweep.
    def _drafting_idle_sweep() -> None:
        """Closure: APScheduler calls this with no args. We capture the
        workspace_controller and app_loop from build_scheduler's scope.

        APScheduler runs jobs in a threadpool by default. The registry's
        asyncio.Lock (and any SDK client state) were created on the FastAPI
        lifespan loop; using asyncio.run() here would create a NEW loop and
        raise RuntimeError (Python 3.12 lazy-binds asyncio primitives to the
        first loop they're awaited on).

        When app_loop is provided (production), we dispatch the coroutine onto
        the lifespan loop via run_coroutine_threadsafe and block until it
        finishes, so APScheduler's max_instances=1 guard is effective.

        When app_loop is None (scheduler-tick unit test), the registry was
        freshly constructed in the test's own loop scope — asyncio.run() is
        safe there and this branch is only exercised in tests."""
        import asyncio

        if app_loop is None:
            # Test/sync construction: fresh registry has no cross-loop concern.
            try:
                asyncio.run(workspace_controller.sweep_idle())
            except Exception:
                _logger.exception("drafting.idle_sweep failed")
            return

        fut = asyncio.run_coroutine_threadsafe(
            workspace_controller.sweep_idle(),
            app_loop,
        )
        try:
            # Block until the sweep completes so APScheduler's max_instances=1
            # prevents the next tick from overlapping with this one.
            fut.result(timeout=300)
        except Exception:
            _logger.exception("drafting.idle_sweep failed")

    sched.add_job(
        _drafting_idle_sweep,
        trigger=IntervalTrigger(
            seconds=settings.workspace_idle_sweep_seconds,
        ),
        id="drafting_idle_sweep",
        max_instances=1,
        coalesce=True,
    )

    if start:
        sched.start()
    return sched
