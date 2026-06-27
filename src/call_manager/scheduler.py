from __future__ import annotations

import contextlib
import logging
from datetime import UTC
from datetime import datetime as _dt
from pathlib import Path
from typing import Protocol

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from call_manager.auth.rate_limit import SlidingWindowLimiter
from call_manager.config import Settings
from call_manager.digest.send import run_digest
from call_manager.email.client import EmailClient
from call_manager.embeddings.client import OllamaEmbedClient
from call_manager.embeddings.refresh import refresh_missing_embeddings_tick
from call_manager.ingestion.alerts import ConsecutiveFailureTracker, check_stale_sources
from call_manager.ingestion.orchestrator import run_ingestion
from call_manager.proposal_assist.analysis.pipeline import drain_analysis_queue
from call_manager.proposal_assist.analysis.reconcile import reconcile_analysis_coverage
from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.phases.setup import run_phase0_tick
from call_manager.ranking.anthropic_client import AnthropicClient
from call_manager.ranking.ollama_client import OllamaClient
from call_manager.ranking.queue import reconcile_missing_rankings
from call_manager.ranking.worker import drain_queue
from call_manager.sources.registry import SOURCES

_logger = logging.getLogger(__name__)

# APScheduler's default thread pool is 10 workers. Several ticks deliberately
# block a worker for a full LLM pass via `fut.result(timeout=...)`
# (_drafting_idle_sweep, _turn_drain_tick, _coherence_tick, _evaluator_tick,
# _review_tick, _uncited_claims_tick, _compaction_tick, the analysis ticks...).
# With ~8-10 of those blocked at once, the default pool can be fully consumed,
# starving the fast crons that MUST run on time — daily ingest, the embedding
# backfill, the weekly digest. Size the pool well above the blocking count so a
# burst of LLM passes always leaves headroom for the cron/DB ticks.
SCHEDULER_EXECUTOR_MAX_WORKERS = 30

# Stable lock key for the single-instance scheduler advisory lock. We hash it
# in SQL via hashtext() so the actual key passed to pg_try_advisory_lock is a
# deterministic 32-bit int — see try_acquire_scheduler_lock().
SCHEDULER_LOCK_NAME = "call-manager-scheduler"


class SchedulerLock:
    """A held Postgres session-level advisory lock for the scheduler.

    The lock is session-level, so it lives exactly as long as the underlying
    DBAPI connection. We therefore keep the SQLAlchemy connection OPEN for the
    process lifetime and only `release()` (which closes it, dropping the lock)
    on app shutdown.
    """

    def __init__(self, connection) -> None:
        self._connection = connection
        self.acquired = True

    def release(self) -> None:
        """Close the held connection, releasing the session-level lock.

        Session-level advisory locks are released automatically when the
        owning connection closes, so closing is sufficient and avoids a
        round-trip to pg_advisory_unlock on a possibly-dead connection.
        """
        if self._connection is None:
            return
        try:
            self._connection.close()
        except Exception:  # noqa: BLE001
            _logger.warning("scheduler lock release: connection close failed", exc_info=True)
        finally:
            self._connection = None
            self.acquired = False


def try_acquire_scheduler_lock(engine: Engine) -> SchedulerLock | None:
    """Try to acquire the process-wide single-instance scheduler lock.

    Opens a dedicated long-lived connection and calls
    `pg_try_advisory_lock(hashtext('call-manager-scheduler'))`. The connection
    is kept open (and returned, wrapped in a SchedulerLock) so the
    session-level lock is held for the process lifetime.

    Returns:
        * a SchedulerLock holding the open connection if the lock was acquired;
        * None if another instance already holds the lock (the probing
          connection is closed), or if the backend doesn't support advisory
          locks / is unreachable (the error is swallowed so startup can't
          crash — only the real Postgres path enforces single-instance).
    """
    try:
        conn = engine.connect()
    except Exception:  # noqa: BLE001
        _logger.warning(
            "scheduler lock: could not open a connection to acquire the advisory "
            "lock; proceeding without single-instance enforcement",
            exc_info=True,
        )
        return None
    try:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"),
                {"name": SCHEDULER_LOCK_NAME},
            ).scalar()
        )
    except Exception:  # noqa: BLE001
        _logger.warning(
            "scheduler lock: pg_try_advisory_lock failed (backend may not support "
            "advisory locks); proceeding without single-instance enforcement",
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            conn.close()
        return None
    if not acquired:
        # Another instance holds the lock — close the probe connection so we
        # don't leak it, and signal "do not start the scheduler".
        with contextlib.suppress(Exception):
            conn.close()
        return None
    return SchedulerLock(conn)


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
    ranking_escalation_min_score: int = 45,
    ranking_remote_daily_cap: int = 100,
    embed_client: OllamaEmbedClient,
    digest_email_client: EmailClient | None = None,
    digest_settings=None,  # duck-typed: needs .digest_dry_run, .admin_dashboard_url
    limiters: list[SlidingWindowLimiter] | None = None,
    workspace_controller: WorkspaceController,
    citation_service=None,  # CitationLLMService | None — gates the analysis ticks
    analysis_embedder=None,  # references.embed.Embedder | None — gates the analysis ticks
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
    # Size the worker pool above the default 10 so blocking LLM ticks can't
    # starve the fast cron/DB ticks (see SCHEDULER_EXECUTOR_MAX_WORKERS).
    sched = _SafeBackgroundScheduler(
        timezone="Europe/Stockholm",
        executors={"default": ThreadPoolExecutor(max_workers=SCHEDULER_EXECUTOR_MAX_WORKERS)},
    )
    # Guard against a silent regression to the default pool size: the blocking
    # ticks need this headroom to coexist with the crons.
    configured_workers = sched._executors["default"]._pool._max_workers
    assert configured_workers == SCHEDULER_EXECUTOR_MAX_WORKERS, (
        f"scheduler executor misconfigured: max_workers={configured_workers}, "
        f"expected {SCHEDULER_EXECUTOR_MAX_WORKERS}"
    )
    _logger.info(
        "scheduler.executor_configured",
        extra={"event": "scheduler_executor_configured", "max_workers": configured_workers},
    )

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
            # APScheduler's default misfire_grace_time is 1 second, so a
            # restart/GC/long-prior-job at the fire instant drops the run
            # entirely. Give daily crons a 6h grace so a late wake still runs.
            misfire_grace_time=6 * 3600,
        )

    # Stale-source backstop: fires once daily (after midnight Stockholm time,
    # after all per-source ingest jobs have had a chance to run).  Alerts if
    # any source has not been successfully fetched in settings.stale_source_alert_days
    # days, regardless of in-memory streak state.  Catches the gap the streak-
    # based alerter misses when the process restarts more often than once every
    # threshold days.
    # Reuse the same EmailSender wired to the alerter via its public accessor.
    # Duck-typed test alerters that don't expose one simply skip the backstop
    # job (they aren't exercising stale-source alerting).
    _stale_sender = getattr(alerter, "sender", None)
    if _stale_sender is not None:
        _stale_admin = settings.admin_alert_email
        _stale_days = settings.stale_source_alert_days

        def _stale_source_check_job() -> None:
            check_stale_sources(
                session_factory=session_factory,
                sender=_stale_sender,
                admin_email=_stale_admin,
                stale_days=_stale_days,
            )

        sched.add_job(
            _stale_source_check_job,
            trigger=CronTrigger(hour=2, minute=30),  # 02:30 Stockholm — after all daily ingest jobs
            id="ingest:stale_check",
            name="Stale-source fetch backstop alert",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=6 * 3600,
        )

    ollama = OllamaClient(base_url=ollama_url, model=gemma_model)
    # Build the remote rank client once if an API key is configured.
    # None ⇒ rank_pair never escalates regardless of score — the safe
    # default for environments without billing or network access to
    # Anthropic. See ranking/anthropic_client.py for the escalation
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
            escalation_min_score=ranking_escalation_min_score,
            remote_daily_cap=ranking_remote_daily_cap,
            batch_size=ranking_queue_batch,
            # Soft per-tick wall-clock budget (B11 Defect 6) so an
            # escalation-heavy batch can't monopolize the scheduler slot.
            tick_budget_seconds=settings.ranking_tick_budget_seconds,
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
        from call_manager.ranking.eval import run_golden_set_eval

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
        misfire_grace_time=6 * 3600,
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
        misfire_grace_time=6 * 3600,
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
        misfire_grace_time=6 * 3600,
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
            # Weekly digest is the worst to drop on a misfire — a restart at
            # Mon 07:00 would skip the whole week. The DigestRun week-uniqueness
            # guard makes a generous grace safe (a late run still de-dupes), so
            # allow a full day of grace to catch a delayed Monday wake.
            misfire_grace_time=24 * 3600,
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
        if not settings.drafting_enabled:
            _logger.info("phase0 tick skipped: DRAFTING_ENABLED=false")
            return
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

    # Profile bootstrap worker: drains profile_bootstrap_jobs.
    def _profile_bootstrap_tick() -> None:
        from call_manager.profiles.bootstrap_worker import run_profile_bootstrap_tick
        from call_manager.profiles.pubmed import PubMedClient
        from call_manager.ranking.ollama_client import OllamaClient

        try:
            run_profile_bootstrap_tick(
                session_factory=session_factory,
                pubmed=PubMedClient(),
                ollama=OllamaClient(base_url=settings.ollama_url, model=settings.gemma_model),
                embed_client=embed_client,
            )
        except Exception as e:  # noqa: BLE001
            _logger.warning(
                "profile_bootstrap.tick_failed",
                extra={"event": "profile_bootstrap_tick_failed", "error": str(e)},
            )

    sched.add_job(
        _profile_bootstrap_tick,
        trigger=IntervalTrigger(seconds=settings.profile_bootstrap_tick_seconds),
        id="profile_bootstrap_tick",
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

    # B4: turn-drain tick. Uses the same app_loop dispatch pattern as
    # drafting_idle_sweep so the registry's loop-bound state isn't
    # corrupted by a worker-thread asyncio.run.
    def _turn_drain_tick() -> None:
        import asyncio
        from pathlib import Path

        from call_manager.proposal_assist.turns.drain import drain_turn_queue_tick

        coro = drain_turn_queue_tick(
            session_factory=session_factory,
            controller=workspace_controller,
            workspaces_dir=Path(settings.workspaces_dir),
            drafting_enabled=settings.drafting_enabled,
            turn_max_seconds=settings.turn_max_seconds,
        )
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("turn_drain tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            # Backstop only: each turn is now bounded in-loop by
            # settings.turn_max_seconds (drain wraps process_turn_once in
            # asyncio.wait_for), so this timeout just caps how long the
            # scheduler thread watches one tick. Give a single slow-but-legal
            # turn room to finish inside the watched window. If a tick with
            # many workspaces still exceeds it, abandoning the watch is now
            # SAFE: the coroutine keeps draining (each turn still bounded) and
            # finalize is status-conditional, so no terminal state is
            # resurrected and no duplicate audit event is emitted.
            fut.result(timeout=settings.turn_max_seconds + 60)
        except Exception:
            _logger.exception("turn_drain tick failed")

    sched.add_job(
        _turn_drain_tick,
        trigger=IntervalTrigger(seconds=settings.turn_drain_tick_seconds),
        id="turn_drain",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-2c: coherence drain tick (async — runs one fresh-stateless keeper pass).
    # Same app_loop dispatch pattern as _turn_drain_tick: the SDK client + asyncio
    # primitives are bound to the FastAPI lifespan loop, so a worker-thread
    # asyncio.run() would create a new loop and break them.
    def _coherence_tick() -> None:
        import asyncio

        from call_manager.proposal_assist.coherence.pass_runner import drain_coherence_queue_tick

        # Skip ONLY on the admin kill-switch (POST also 503s on it, so queued work
        # waits for re-enable). Do NOT skip on a missing anthropic_api_key: the spec
        # deliberately has no API-key HTTP gate, so a misconfigured provider must
        # surface as a FAILED job at runtime (the pass errors → job failed), not a
        # job stuck queued forever with the SPA polling indefinitely.
        if not settings.drafting_enabled:
            return
        coro = drain_coherence_queue_tick(session_factory=session_factory, settings=settings)
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("coherence tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=settings.coherence_pass_timeout_seconds + 60)
        except Exception:
            _logger.exception("coherence tick failed")

    sched.add_job(
        _coherence_tick,
        trigger=IntervalTrigger(seconds=settings.coherence_tick_seconds),
        id="coherence_drain",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-2c: coherence reaper tick (sync, pure DB). Runs even while _coherence_tick
    # is blocked inside a live pass, so a crashed/hung running job can't wedge the
    # workspace's active-unique slot forever. Mirrors _turn_reaper_tick.
    def _coherence_reaper_tick() -> None:
        from call_manager.proposal_assist.coherence.jobs import reap_stuck_coherence_jobs

        try:
            with session_factory() as s:
                reap_stuck_coherence_jobs(s, timeout_seconds=settings.coherence_job_timeout_seconds)
                s.commit()
        except Exception:
            _logger.exception("coherence reaper tick failed")

    # Sweep cadence is DECOUPLED from the TTL age cutoff: running every
    # coherence_reaper_tick_seconds (default 60s) while the cutoff stays at
    # coherence_job_timeout_seconds (default 300s) caps worst-case unwedge after a
    # crash at ~TTL + tick (~360s) rather than ~2x TTL (~600s) when they're equal.
    # The in-pass timeout is still the primary bound; this reaper is crash recovery.
    sched.add_job(
        _coherence_reaper_tick,
        trigger=IntervalTrigger(seconds=settings.coherence_reaper_tick_seconds),
        id="coherence_reaper",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-3a: evaluator drain tick (async — one fresh-stateless scorecard pass).
    # Same app_loop dispatch pattern as _coherence_tick: the SDK client + asyncio
    # primitives are bound to the FastAPI lifespan loop, so a worker-thread
    # asyncio.run() would create a new loop and break them.
    def _evaluator_tick() -> None:
        import asyncio

        from call_manager.proposal_assist.evaluator.pass_runner import (
            drain_evaluator_queue_tick,
        )

        # Skip ONLY on the admin kill-switch (mirrors _coherence_tick): do NOT skip
        # on a missing anthropic_api_key so a misconfigured provider surfaces as a
        # FAILED job rather than a job stuck queued forever.
        if not settings.drafting_enabled:
            return
        coro = drain_evaluator_queue_tick(session_factory=session_factory, settings=settings)
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("evaluator tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=settings.evaluator_pass_timeout_seconds + 60)
        except Exception:
            _logger.exception("evaluator tick failed")

    sched.add_job(
        _evaluator_tick,
        trigger=IntervalTrigger(seconds=settings.evaluator_tick_seconds),
        id="evaluator_drain",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-3a: evaluator reaper tick (sync, pure DB). Crash/hung-job recovery.
    # Runs even while _evaluator_tick is blocked inside a live pass, so a
    # crashed/hung running job can't wedge the workspace's active-unique slot
    # forever. Mirrors _coherence_reaper_tick.
    def _evaluator_reaper_tick() -> None:
        from call_manager.proposal_assist.evaluator.jobs import reap_stuck_evaluator_jobs

        try:
            with session_factory() as s:
                reap_stuck_evaluator_jobs(s, timeout_seconds=settings.evaluator_job_timeout_seconds)
                s.commit()
        except Exception:
            _logger.exception("evaluator reaper tick failed")

    sched.add_job(
        _evaluator_reaper_tick,
        trigger=IntervalTrigger(seconds=settings.evaluator_reaper_tick_seconds),
        id="evaluator_reaper",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-3c: review drain tick (async — one fresh-stateless mock-panel fan-out).
    def _review_tick() -> None:
        import asyncio

        from call_manager.proposal_assist.review.pass_runner import drain_review_queue_tick

        if not settings.drafting_enabled:
            return
        coro = drain_review_queue_tick(session_factory=session_factory, settings=settings)
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("review tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=settings.review_job_timeout_seconds + 60)
        except Exception:
            _logger.exception("review tick failed")

    sched.add_job(
        _review_tick,
        trigger=IntervalTrigger(seconds=settings.review_tick_seconds),
        id="review_drain",
        max_instances=1,
        coalesce=True,
    )

    def _review_reaper_tick() -> None:
        from call_manager.proposal_assist.review.jobs import reap_stuck_review_runs

        try:
            with session_factory() as s:
                reap_stuck_review_runs(s, timeout_seconds=settings.review_job_timeout_seconds)
                s.commit()
        except Exception:
            _logger.exception("review reaper tick failed")

    sched.add_job(
        _review_reaper_tick,
        trigger=IntervalTrigger(seconds=settings.review_reaper_tick_seconds),
        id="review_reaper",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-3d: uncited-claims drain tick (async — one fresh-stateless pass).
    def _uncited_claims_tick() -> None:
        import asyncio

        from call_manager.proposal_assist.uncited_claims.pass_runner import (
            drain_uncited_claims_queue_tick,
        )

        if not settings.drafting_enabled:
            return
        coro = drain_uncited_claims_queue_tick(session_factory=session_factory, settings=settings)
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("uncited-claims tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=settings.uncited_claims_pass_timeout_seconds + 60)
        except Exception:
            _logger.exception("uncited-claims tick failed")

    sched.add_job(
        _uncited_claims_tick,
        trigger=IntervalTrigger(seconds=settings.uncited_claims_tick_seconds),
        id="uncited_claims_drain",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-3d: uncited-claims reaper tick (sync, pure DB). Crash/hung-job recovery.
    def _uncited_claims_reaper_tick() -> None:
        from call_manager.proposal_assist.uncited_claims.jobs import (
            reap_stuck_uncited_claims_jobs,
        )

        try:
            with session_factory() as s:
                reap_stuck_uncited_claims_jobs(
                    s, timeout_seconds=settings.uncited_claims_job_timeout_seconds
                )
                s.commit()
        except Exception:
            _logger.exception("uncited-claims reaper tick failed")

    sched.add_job(
        _uncited_claims_reaper_tick,
        trigger=IntervalTrigger(seconds=settings.uncited_claims_reaper_tick_seconds),
        id="uncited_claims_reaper",
        max_instances=1,
        coalesce=True,
    )

    # Stuck-turn reaper. A turn cancelled by _turn_drain_tick's
    # fut.result(timeout=300) keeps running on the app loop with the handle
    # cached and its turn_jobs row stuck status='running'. The drain query
    # excludes that workspace forever (orphan sweep only runs on a COLD
    # launch). This periodic sweep marks any 'running' row older than the TTL
    # as 'failed' so the workspace unwedges and the next drain can proceed.
    # Pure DB work — runs synchronously in APScheduler's threadpool with its
    # own short-lived session, so no app_loop dispatch is needed.
    def _turn_reaper_tick() -> None:
        from call_manager.proposal_assist.turns.reaper import reap_stuck_turns

        try:
            reap_stuck_turns(session_factory=session_factory)
        except Exception:
            _logger.exception("turn_reaper tick failed")

    sched.add_job(
        _turn_reaper_tick,
        # Run every 5 minutes. The TTL (15 min default) is the real safety
        # bound; the tick cadence just determines how soon after the TTL a
        # wedged workspace gets freed.
        trigger=IntervalTrigger(seconds=300),
        id="turn_reaper",
        max_instances=1,
        coalesce=True,
    )

    # B4: compaction tick. Synchronous (just DB + Anthropic HTTP call).
    def _compaction_tick() -> None:
        if not settings.drafting_enabled:
            _logger.info("compaction tick skipped: DRAFTING_ENABLED=false")
            return

        from call_manager.proposal_assist.turns.compaction import run_compaction_tick

        def make_summarizer_client() -> AnthropicClient:
            return AnthropicClient(
                api_key=settings.anthropic_api_key,
                model=settings.proposal_assist_llm_model,
            )

        if not settings.anthropic_api_key:
            _logger.info("compaction tick skipped: ANTHROPIC_API_KEY not set")
            return

        try:
            run_compaction_tick(
                session_factory=session_factory,
                summarizer_client_factory=make_summarizer_client,
                settings=settings,
            )
        except Exception:
            _logger.exception("compaction tick failed")

    sched.add_job(
        _compaction_tick,
        trigger=IntervalTrigger(seconds=settings.compaction_tick_seconds),
        id="compaction_tick",
        max_instances=1,
        coalesce=True,
    )

    # Claim-analysis ticks (spec 2026-06-10 §3). Pure sync DB + LLM-HTTP work,
    # so they run directly in APScheduler's threadpool — no app_loop dispatch.
    def _analysis_drain_tick() -> None:
        try:
            out = drain_analysis_queue(
                session_factory=session_factory,
                citation_service=citation_service,
                embedder=analysis_embedder,
                workspaces_dir=Path(settings.workspaces_dir),
            )
            if any(out.values()):
                _logger.info("analysis drain: %s", out)
        except Exception:
            _logger.exception("analysis drain tick failed")

    def _analysis_reconcile_tick() -> None:
        try:
            n = reconcile_analysis_coverage(
                session_factory=session_factory,
                workspaces_dir=Path(settings.workspaces_dir),
            )
            if n:
                _logger.info("analysis reconcile enqueued %d", n)
        except Exception:
            _logger.exception("analysis reconcile tick failed")

    # Gate on citation-service + embedder AVAILABILITY, not ANTHROPIC_API_KEY:
    # the citation LLM defaults to local Ollama (CITATION_LLM_PROVIDER=ollama),
    # so an Anthropic key is irrelevant here. main.py always passes both in
    # production (build_citation_service raises at create_app() on misconfig;
    # the embedder wraps the always-constructed embed_client) — None means a
    # caller (tests, future config) deliberately left analysis unwired.
    if settings.drafting_enabled and citation_service is not None and analysis_embedder is not None:
        sched.add_job(
            _analysis_drain_tick,
            trigger=IntervalTrigger(seconds=settings.analysis_tick_seconds),
            id="claim_analysis_drain",
            max_instances=1,
            coalesce=True,
        )
        sched.add_job(
            _analysis_reconcile_tick,
            trigger=IntervalTrigger(seconds=settings.analysis_reconcile_tick_seconds),
            id="claim_analysis_reconcile",
            max_instances=1,
            coalesce=True,
        )
    else:
        _logger.info(
            "claim-analysis ticks skipped (drafting_enabled=%s, "
            "citation_service=%s, analysis_embedder=%s)",
            settings.drafting_enabled,
            citation_service is not None,
            analysis_embedder is not None,
        )

    if start:
        sched.start()
    return sched


def trigger_job_now(scheduler: BackgroundScheduler | None, job_id: str) -> bool:
    """Ask APScheduler to run the given job once, immediately, then resume its
    normal schedule. Returns False if the scheduler or the job is absent (e.g.
    the digest job when no email backend is configured).

    The job runs in APScheduler's background thread, reusing the exact tick
    closure with its already-wired clients. The jobs carry max_instances=1 +
    coalesce=True, so a double-trigger collapses to one run.
    """
    if scheduler is None:
        return False
    job = scheduler.get_job(job_id)
    if job is None:
        return False
    job.modify(next_run_time=_dt.now(UTC))
    return True
