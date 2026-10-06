from __future__ import annotations

import contextlib
import logging
import threading
from datetime import UTC
from datetime import datetime as _dt
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from grant_lab.auth.rate_limit import SlidingWindowLimiter
from grant_lab.boards.alerts import send_board_alerts
from grant_lab.boards.auto_publish import reconcile_auto_boards
from grant_lab.config import Settings
from grant_lab.digest.send import run_digest
from grant_lab.email.client import EmailClient
from grant_lab.embeddings.client import OllamaEmbedClient
from grant_lab.embeddings.refresh import refresh_missing_embeddings_tick
from grant_lab.ingestion.alerts import ConsecutiveFailureTracker, check_stale_sources
from grant_lab.ingestion.orchestrator import run_ingestion
from grant_lab.ranking.anthropic_client import AnthropicClient
from grant_lab.ranking.freshness import FreshnessContext, ScoringConfig

if TYPE_CHECKING:
    # Annotation only — `from __future__ import annotations` makes it a string,
    # so this never executes at runtime and a match process never imports it.
    from grant_lab.proposal_assist.drafting.controller import WorkspaceController
from grant_lab.ranking.ollama_client import OllamaClient
from grant_lab.ranking.queue import reconcile_missing_rankings
from grant_lab.ranking.worker import drain_queue
from grant_lab.sources.registry import SOURCES

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
        self._ping_lock = threading.Lock()

    def is_held(self) -> bool:
        """Is the lock still ours? Pings the held connection: the lock is
        session-level, so it lives exactly as long as that session. A dead
        session (the backend was terminated, Postgres restarted) means the lock
        is gone and another instance may already hold it — which used to be
        silent (review 2026-09-02 A9 S5). Logged ONCE; after that the answer is
        False without another ping. Serialized: /healthz is called concurrently
        and a DBAPI connection is not."""
        with self._ping_lock:
            if not self.acquired or self._connection is None:
                return False
            try:
                self._connection.execute(text("SELECT 1"))
            except Exception:  # noqa: BLE001 — any failure means the session is gone
                self.acquired = False
                _logger.warning(
                    "scheduler.lock_lost",
                    extra={
                        "event": "scheduler_lock_lost",
                        "detail": (
                            "the connection holding the single-instance advisory lock "
                            "is dead; another instance can now take the lock while this "
                            "one keeps running its scheduler — restart this instance"
                        ),
                    },
                )
                return False
            return True

    def release(self) -> None:
        """Explicitly drop the session-level advisory lock, then close the connection.

        We do NOT rely on `conn.close()` alone to free the lock: the lock was
        taken on a connection borrowed from the SHARED QueuePool, and on a pool
        `close()` returns the underlying DBAPI connection to the pool (with a
        ROLLBACK reset) rather than physically closing it — and a session-level
        `pg_advisory_lock` survives ROLLBACK. So the lock would actually linger
        (held by the pooled connection) until `engine.dispose()` physically
        closes it. To make release reliable and self-contained, we run
        `pg_advisory_unlock(hashtext(...))` on the held connection FIRST, then
        close it. The unlock is best-effort (swallowed on a dead connection):
        if the connection is already gone, Postgres has released the lock
        server-side anyway.
        """
        if self._connection is None:
            return
        try:
            with contextlib.suppress(Exception):
                self._connection.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:name))"),
                    {"name": SCHEDULER_LOCK_NAME},
                )
            invalidate = getattr(self._connection, "invalidate", None)
            if invalidate is not None:
                with contextlib.suppress(Exception):
                    invalidate()
            self._connection.close()
        except Exception:  # noqa: BLE001
            _logger.warning("scheduler lock release: connection close failed", exc_info=True)
        finally:
            self._connection = None
            self.acquired = False


class SingleInstanceViolation(RuntimeError):
    """Raised at startup when another process holds the single-instance lock and
    SCHEDULER_STANDBY_OK is not set. Uvicorn turns a lifespan exception into
    "Application startup failed. Exiting." with a non-zero exit."""


def try_acquire_scheduler_lock(engine: Engine) -> SchedulerLock | None:
    """`probe_scheduler_lock` without the reason — the original entry point,
    kept for every caller and test that only needs the lock or None."""
    lock, _state = probe_scheduler_lock(engine)
    return lock


def probe_scheduler_lock(engine: Engine) -> tuple[SchedulerLock | None, str]:
    """Try to acquire the process-wide single-instance scheduler lock, and say
    why when it could not: `"acquired"`, `"held_elsewhere"` (another process
    holds it — a second instance, which the lifespan treats as FATAL unless
    SCHEDULER_STANDBY_OK), or `"unavailable"` (the backend could not answer the
    probe — an outage, tolerated with a warning so /healthz can report it).

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
        return None, "unavailable"
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
        return None, "unavailable"
    if not acquired:
        # Another instance holds the lock — close the probe connection so we
        # don't leak it, and signal "do not start the scheduler".
        with contextlib.suppress(Exception):
            conn.close()
        return None, "held_elsewhere"
    return SchedulerLock(conn), "acquired"


#: How often the watchdog asks whether we still own the single-instance lock.
#: Between two checks this process can be a second scheduler, so the interval is
#: the width of that window — short enough to matter, long enough that the ping
#: is noise on the connection the lock already holds open.
SCHEDULER_LOCK_WATCHDOG_SECONDS = 30.0


def enforce_scheduler_lock(lock: SchedulerLock, scheduler: BackgroundScheduler) -> bool:
    """One ownership check. True while the lock is ours; on loss, stop the scheduler.

    Losing the lock is not "maybe". The lock is session-level, so a dead session
    has ALREADY released it server-side and another instance can take it while
    this one keeps firing ingestion, the digest and the ranking drain — exactly
    the second scheduler the single-instance constraint exists to prevent
    (CLAUDE.md, and ``build_scheduler``'s own docstring). Until this existed,
    nothing acted on ``is_held()`` at all: the only caller was the /healthz
    field, which reports the loss to a monitor and changes nothing here.

    Deliberately does NOT re-acquire. Re-acquiring would be *safe* — a
    successful ``pg_try_advisory_lock`` proves nobody else holds it — but a
    shut-down APScheduler cannot be restarted, so resuming means rebuilding the
    scheduler and rewiring every tick. That is a larger change than the safety
    property needs. The log line says restart the process, and it means it.
    """
    if lock.is_held():
        return True
    _logger.error(
        "scheduler.lock_lost_stopping",
        extra={
            "event": "scheduler_lock_lost_stopping",
            "detail": (
                "the connection holding the single-instance advisory lock is dead, "
                "so Postgres has released the lock and another instance may already "
                "hold it — stopping this process's scheduler. HTTP keeps serving and "
                "/healthz reports scheduler_lock=false. Restart this instance to "
                "resume scheduled work."
            ),
        },
    )
    scheduler.shutdown(wait=False)
    return False


def start_scheduler_lock_watchdog(
    lock: SchedulerLock,
    scheduler: BackgroundScheduler,
    *,
    interval_seconds: float = SCHEDULER_LOCK_WATCHDOG_SECONDS,
    stop_event: threading.Event | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Run ``enforce_scheduler_lock`` on a daemon thread until it says stop.

    A plain thread, not an APScheduler job: a job that shuts down its own
    scheduler is reentrancy nobody should have to reason about, and this check
    must keep working even when every executor worker is blocked on an LLM pass
    — which is the whole reason SCHEDULER_EXECUTOR_MAX_WORKERS is 30.

    Returns the thread and the event that stops it, so the lifespan can join it
    before tearing the scheduler down.
    """
    stop = stop_event if stop_event is not None else threading.Event()

    def _run() -> None:
        while not stop.wait(interval_seconds):
            try:
                if not enforce_scheduler_lock(lock, scheduler):
                    return
            except Exception:  # noqa: BLE001 — a watchdog that dies is worse than a slow one
                _logger.exception("scheduler lock watchdog tick failed")

    thread = threading.Thread(target=_run, name="scheduler-lock-watchdog", daemon=True)
    thread.start()
    return thread, stop


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


def _drain_pass_future(fut, *, pass_name: str, timeout: float) -> None:
    """Drain a pass tick's future, distinguishing a DELIBERATE retirement from a
    failure.

    concurrent.futures.CancelledError must be caught BEFORE `except Exception`,
    because — unlike asyncio.CancelledError — it IS an Exception subclass. Without
    this, a workspace delete cancelling a live pass is logged as a crashed tick with
    a full traceback, training operators to ignore the line that matters.
    """
    import concurrent.futures

    try:
        fut.result(timeout=timeout)
    except concurrent.futures.CancelledError:
        _logger.info(
            "%s tick pass retired (workspace deleted)",
            pass_name,
            extra={"event": "pass_tick_retired", "pass": pass_name},
        )
    except Exception:
        _logger.exception("%s tick failed", pass_name)


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
    remote_rank_model: str = "claude-sonnet-5-5",
    ranking_escalation_min_score: int = 45,
    ranking_remote_daily_cap: int = 300,
    embed_client: OllamaEmbedClient,
    digest_email_client: EmailClient | None = None,
    digest_settings=None,  # duck-typed: needs .digest_dry_run, .admin_dashboard_url
    limiters: list[SlidingWindowLimiter] | None = None,
    # None in a MATCH boot: main.py's lifespan builds the proposal-assist half only
    # when settings.drafting_enabled, and match forbids drafting. Every tick below
    # that touches it guards on the same flag.
    workspace_controller: WorkspaceController | None = None,
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
    # Best-effort observability of the configured pool size. This reaches into
    # private APScheduler internals (executors -> _pool -> _max_workers), so it
    # is defensive: an APScheduler upgrade that renames any of these must NOT
    # crash startup with an AttributeError. The actual configuration is asserted
    # by the unit test (test_scheduler), so we only LOG here — no runtime assert.
    configured_workers: int | None = None
    try:
        _default_exec = sched._executors.get("default")
        configured_workers = getattr(getattr(_default_exec, "_pool", None), "_max_workers", None)
    except Exception:  # noqa: BLE001 — purely observational, never fatal
        configured_workers = None
    _logger.info(
        "scheduler.executor_configured",
        extra={
            "event": "scheduler_executor_configured",
            "max_workers": configured_workers,
            "expected_max_workers": SCHEDULER_EXECUTOR_MAX_WORKERS,
        },
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

    # Ranking freshness (spec D3/D4): ingest and reconcile judge "current" with
    # the SAME scorer values the rank:drain job below hands drain_queue. Built
    # from these locals, never from Settings, so the two cannot drift (gate G26).
    freshness = FreshnessContext(
        scoring=ScoringConfig.for_drain(
            client=ollama,
            embedding_threshold=embedding_pregate_threshold,
            embedding_hard_fail_threshold=embedding_hard_fail_threshold,
            escalation_min_score=ranking_escalation_min_score,
            logprobs_enabled=settings.ranking_logprobs_enabled,
        ),
        remote_enabled=anthropic_client is not None,
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
                freshness=freshness,
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
            logprobs_enabled=settings.ranking_logprobs_enabled,
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

    if settings.collab_checkpoint_scan_seconds > 0:

        def _collab_checkpoint_job() -> None:
            from grant_lab.proposal_assist.collab.checkpoint import checkpoint_active_tails
            from grant_lab.proposal_assist.collab.handover import run_kill_switch_handover

            if settings.collab_kill_switch:
                # Kill-switch engaged: retry the drain-then-truncate handover for
                # artifacts whose startup pass failed (no tail floor). Normal
                # tail-bounding is pointless while connects are refused.
                run_kill_switch_handover(session_factory, workspaces_dir=settings.workspaces_dir)
                return
            checkpoint_active_tails(
                session_factory,
                workspaces_dir=settings.workspaces_dir,
                min_tail=settings.collab_checkpoint_min_tail,
            )

        sched.add_job(
            _collab_checkpoint_job,
            trigger=IntervalTrigger(seconds=settings.collab_checkpoint_scan_seconds),
            id="collab:checkpoint_tails",
            name="Collab active-session tail-bounding checkpoint",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    def _golden_tick() -> None:
        # Lazy import: keeps scheduler module-load cheap and dodges any
        # potential cycle via ranking → db → web. The import only fires
        # when the scheduled job actually runs (weekly).
        from grant_lab.ranking.eval import run_golden_set_eval

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

    # Where yesterday's tick stopped in the PROFILE table. `_TICK_MAX_BATCHES`
    # bounds the drain, and a tick that stops on that cap has rows still ahead
    # of it; restarting at 0 tomorrow would re-walk the same prefix and halt at
    # the same wall, so a profile beyond it would never be repaired. Since the
    # profile scan was widened (spec 5(a)) it is a scan of the WHOLE table, so
    # a long prefix of fresh profiles can consume the whole allowance.
    #
    # The CALL cursor is carried for the same reason, since migration 0080
    # (spec 2026-09-08 D9). Until then the call half was narrowed in SQL
    # (`embedding IS NULL OR embedding_model != current`): a call that embedded
    # was never returned again, a capped call drain had recorded its progress
    # in the DATA, and restarting at 0 cost nothing — so no call cursor was
    # carried. The call half now examines every call row, as the profile half
    # does, and a tick that restarted it at 0 could never reach past
    # `batch_size * _TICK_MAX_BATCHES` (40,000) calls. Carrying it also ends
    # the old bound on persistently-failing calls, which stayed in the SQL
    # candidate set and re-consumed the allowance on every tick ahead of the
    # rows behind them: the cursor now passes a failed call like any other, and
    # it is retried once per full pass of the table, when the cursor resets.
    #
    # Both are held in this closure, not in the database. The scheduler is
    # in-process and single-instance (a hard constraint), so one process owns
    # these values and no other writer exists; the repo has no settings/KV
    # table to put them in and adding one for a resume hint would be the larger
    # change. The cost is that a process restart resets both to 0 — the next
    # tick re-walks from the start, which is slower but never skips a row.
    embeddings_after_call_id = 0
    embeddings_after_profile_id = 0

    def _embeddings_tick() -> None:
        nonlocal embeddings_after_call_id, embeddings_after_profile_id
        try:
            summary = refresh_missing_embeddings_tick(
                session_factory=session_factory,
                embed_client=embed_client,
                batch_size=200,
                after_call_id=embeddings_after_call_id,
                after_profile_id=embeddings_after_profile_id,
            )
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "embeddings.refresh_missing_tick.failed",
                extra={"event": "embedding_refresh_tick_failed", "error": str(e)},
            )
            return
        # Reset ONLY on an exhausted scan — a cursor that never went back to 0
        # would be a worse bug than the one it fixes: a profile edited BEHIND
        # the high-water mark would be permanently unreachable.
        #
        # Keyed on the PROFILE half's own exhaustion, NOT on the drain's joint
        # `scan_exhausted`. The joint flag sums both halves, so a tick held at
        # `max_batches` by the CALL half (then: a call that kept failing to
        # embed stayed in the SQL candidate set and was re-examined every
        # batch; since 0080: more calls past the call cursor than one tick
        # examines) reports False even when the profile half ran off the end
        # of its own table on the first batch. Keying on it froze this cursor
        # past the last profile id for as long as any call kept failing —
        # measured after_profile_id values [0, 2, 2, 2, 2, 2] across six ticks
        # — so a profile that went stale behind it was never repaired. Pinned
        # by tests/test_scheduler_embedding_cursor.py (gate 9c).
        embeddings_after_profile_id = (
            0 if summary["profile_scan_exhausted"] else summary["max_profile_id"]
        )
        # The call cursor, by the same rule and for both of the same reasons,
        # each mirrored. Reset only on an exhausted CALL scan: a call re-ingested
        # (or failing to embed) behind the high-water mark must be reachable
        # again. And keyed on the CALL half's own `call_scan_exhausted`, never
        # the joint flag: a tick held at the cap by the PROFILE half reports
        # `scan_exhausted` False while the call scan ran off the end of its table
        # on the first batch, which would freeze this cursor past the last call
        # id — gate 9c's bug the other way round. Pinned by G6-G8 in the same
        # test module (G8 is 9c's mirror).
        embeddings_after_call_id = 0 if summary["call_scan_exhausted"] else summary["max_call_id"]
        # No need to re-log the summary — refresh_missing_embeddings_tick already does.

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
        # daily sweep enqueues any (open call × active profile) pair
        # whose ranking is missing or no longer current, and that has no
        # active RankingJob.
        try:
            with session_factory() as session:
                n = reconcile_missing_rankings(session, ctx=freshness)
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

    def _auto_publish_tick() -> None:
        # 05:00, after the 04:00 reconcile has enqueued ranking work. The
        # ranking queue drains on an INTERVAL (RANKING_TICK_SECONDS, default
        # 60s), so there is no instant at which ranking is "finished" and no
        # completion signal to wait for. This runs on the clock instead, is
        # idempotent, and is correct if it runs mid-drain: it publishes what
        # the scores say now, and tomorrow corrects it.
        try:
            summary = reconcile_auto_boards(session_factory=session_factory)
            _logger.info(
                "boards.auto_publish.tick.done",
                extra={
                    "event": "boards_auto_publish_tick_done",
                    "boards_seen": summary.boards_seen,
                    "rows_added": summary.rows_added,
                    "rows_removed": summary.rows_removed,
                    "boards_failed": summary.boards_failed,
                },
            )
        except Exception as e:  # noqa: BLE001
            _logger.exception(
                "boards.auto_publish.failed",
                extra={"event": "boards_auto_publish_failed", "error": str(e)},
            )

    sched.add_job(
        _auto_publish_tick,
        trigger=CronTrigger(hour=5, minute=0),
        id="boards:auto-publish",
        name="Daily rebuild of self-publishing corridor boards",
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

            # D9 (Plan C Task 8, Important 1): board alerts ride the SAME
            # weekly cadence as the digest -- no new job, lock or route. Its
            # own try/except so a board-alert failure is never folded into
            # "digest.tick.error" (that event already means something
            # specific to on-call) and never stops the digest send above.
            try:
                board_summary = send_board_alerts(
                    session_factory=session_factory,
                    email_client=digest_email_client,
                    settings=digest_settings,
                )
                _logger.info(
                    "board_alerts.tick.done",
                    extra={
                        "event": "board_alerts_tick_done",
                        "alerts_found": board_summary.alerts_found,
                        "messages_sent": board_summary.messages_sent,
                        "bounces": board_summary.bounces,
                        "transport_errors": board_summary.transport_errors,
                        "no_recipients": board_summary.no_recipients,
                    },
                )
            except Exception as e:  # noqa: BLE001
                _logger.exception(
                    "board_alerts.tick.error",
                    extra={"event": "board_alerts_tick_error", "error": str(e)},
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
            # allow a full day of grace to catch a delayed Monday wake. This
            # grace is now also shared with `send_board_alerts` (Task 8, D9),
            # which has NO idempotency record of its own (Minor 1, Task 8
            # review round 2) — a late-firing misfire replays the board-alert
            # send too, which is harmless only because it is not idempotent
            # by omission but by intent (an ongoing shortfall is meant to be
            # re-reported next week; see `send_board_alerts`'s own docstring).
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
            # The job's MAX_SETUP_ATTEMPTS is the retry. SDK retries on top made
            # each attempt up to three billed Opus calls (review 2026-10-06 H2),
            # exactly as compaction's tick found.
            return AnthropicClient(
                api_key=settings.anthropic_api_key,
                model=settings.proposal_assist_llm_model,
                max_retries=0,
            )

        # Deferred: importing the proposal-assist stack at module scope made a match
        # process load pycrdt and prosemirror at boot for jobs it never runs.
        # Pinned by tests/architecture/test_match_edition_process_cut.py.
        from grant_lab.proposal_assist.phases.setup import run_phase0_tick

        run_phase0_tick(
            session_factory=session_factory,
            client_factory=make_client,
            extractor_version=settings.extractor_version,
            rubric_extractor_version=settings.rubric_extractor_version,
            workspaces_dir=settings.workspaces_dir,
            settings=settings,
        )

    sched.add_job(
        _phase0_tick,
        trigger=IntervalTrigger(seconds=settings.phase0_tick_seconds),
        id="proposal_assist_phase0_tick",
        max_instances=1,
        coalesce=True,
    )

    # Jobs-retirement reconciliation. NOT gated on drafting_enabled or the
    # Anthropic key: it needs no LLM client, and rows the "jobs" hook skipped
    # would otherwise never converge on a deployment with drafting off.
    def _jobs_reconcile_tick() -> None:
        from grant_lab.proposal_assist.jobs_retirement import reconcile_cancelled_jobs

        with session_factory() as session:
            n = reconcile_cancelled_jobs(session)
            session.commit()
        if n:
            _logger.info("jobs retirement reconcile: %d row(s)", n)

    sched.add_job(
        _jobs_reconcile_tick,
        trigger=IntervalTrigger(seconds=settings.jobs_reconcile_tick_seconds),
        id="jobs_retirement_reconcile_tick",
        max_instances=1,
        coalesce=True,
    )

    # Unconfirmed pass generations. A SIBLING of reconcile_live_clients, not an edit
    # to it: that sweep covers the interactive registry's adapter, and these one-shot
    # pass adapters are built per-pass, so nothing else owns them.
    #
    # A SYNC closure that dispatches onto app_loop — this is a BackgroundScheduler
    # running sync callables, so registering an `async def` directly would hand it a
    # coroutine nobody awaits: green unit test, dead production path.
    #
    # NOT gated on drafting_enabled or the Anthropic key: hiding it there would
    # strand every unconfirmed generation on a deployment with drafting off.
    def _passes_reconcile_tick() -> None:
        import asyncio

        from grant_lab.proposal_assist.passes import PASS_REGISTRY

        coro = PASS_REGISTRY.reconcile_unconfirmed()
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("passes reconcile tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            n = fut.result(timeout=30)
        except Exception:
            _logger.exception("passes reconcile tick failed")
            return
        if n:
            _logger.info("passes reconcile: %d entry(ies)", n)

    sched.add_job(
        _passes_reconcile_tick,
        trigger=IntervalTrigger(seconds=settings.passes_reconcile_tick_seconds),
        id="passes_reconcile_tick",
        max_instances=1,
        coalesce=True,
    )

    # Profile bootstrap worker: drains profile_bootstrap_jobs.
    def _profile_bootstrap_tick() -> None:
        from grant_lab.profiles.bootstrap_worker import run_profile_bootstrap_tick
        from grant_lab.profiles.pubmed import PubMedClient
        from grant_lab.ranking.ollama_client import OllamaClient

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
        safe there and this branch is only exercised in tests.

        Guarded on drafting_enabled like its seven sibling ticks. It was the only
        one that was not, so a MATCH deployment registered this job and fired it
        on every interval against the drafting registry it does not serve. It is
        also the only tick that dereferences workspace_controller without the
        flag check, so the guard is what makes the parameter safely optional."""
        if not settings.drafting_enabled:
            return

        import asyncio

        async def _sweep_and_reconcile():
            await workspace_controller.sweep_idle()
            # Same tick: retry anything a deletion or a failed teardown left behind
            # (Slice A). Cheap when there is nothing to do.
            await workspace_controller.reconcile_live_clients()

        if app_loop is None:
            # Test/sync construction: fresh registry has no cross-loop concern.
            try:
                asyncio.run(_sweep_and_reconcile())
            except Exception:
                _logger.exception("drafting.idle_sweep failed")
            return

        fut = asyncio.run_coroutine_threadsafe(
            _sweep_and_reconcile(),
            app_loop,
        )
        try:
            # Block until the sweep completes so APScheduler's max_instances=1
            # prevents the next tick from overlapping with this one.
            fut.result(timeout=300)
        except Exception:
            _logger.exception("drafting.idle_sweep failed")

    # Registered only when drafting is on, so a MATCH scheduler does not carry a
    # job that can never do anything. Gating the REGISTRATION as well as the body
    # is what makes this observable from outside the process: the architecture
    # gate asserts the job id is absent in match and present in full, which an
    # early-returning body would not show.
    if settings.drafting_enabled:
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

        from grant_lab.proposal_assist.turns.drain import drain_turn_queue_tick

        coro = drain_turn_queue_tick(
            session_factory=session_factory,
            controller=workspace_controller,
            workspaces_dir=Path(settings.workspaces_dir),
            drafting_enabled=settings.drafting_enabled,
            turn_max_seconds=settings.turn_max_seconds,
            settings=settings,
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

        from grant_lab.proposal_assist.coherence.pass_runner import drain_coherence_queue_tick

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
        _drain_pass_future(
            fut, pass_name="coherence", timeout=settings.coherence_pass_timeout_seconds + 60
        )

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
        from grant_lab.proposal_assist.coherence.jobs import reap_stuck_coherence_jobs

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

        from grant_lab.proposal_assist.evaluator.pass_runner import (
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
        _drain_pass_future(
            fut, pass_name="evaluator", timeout=settings.evaluator_pass_timeout_seconds + 60
        )

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
        from grant_lab.proposal_assist.evaluator.jobs import reap_stuck_evaluator_jobs

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

        from grant_lab.proposal_assist.review.pass_runner import drain_review_queue_tick

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
        _drain_pass_future(
            fut, pass_name="review", timeout=settings.review_job_timeout_seconds + 60
        )

    sched.add_job(
        _review_tick,
        trigger=IntervalTrigger(seconds=settings.review_tick_seconds),
        id="review_drain",
        max_instances=1,
        coalesce=True,
    )

    def _review_reaper_tick() -> None:
        from grant_lab.proposal_assist.review.jobs import reap_stuck_review_runs

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

        from grant_lab.proposal_assist.uncited_claims.pass_runner import (
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
        _drain_pass_future(
            fut,
            pass_name="uncited_claims",
            timeout=settings.uncited_claims_pass_timeout_seconds + 60,
        )

    sched.add_job(
        _uncited_claims_tick,
        trigger=IntervalTrigger(seconds=settings.uncited_claims_tick_seconds),
        id="uncited_claims_drain",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-3d: uncited-claims reaper tick (sync, pure DB). Crash/hung-job recovery.
    def _uncited_claims_reaper_tick() -> None:
        from grant_lab.proposal_assist.uncited_claims.jobs import (
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
        from grant_lab.proposal_assist.turns.reaper import reap_stuck_turns

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

        from grant_lab.proposal_assist.turns.compaction import run_compaction_tick

        def make_summarizer_client() -> AnthropicClient:
            # The tick IS the retry (every 300 s, capped by
            # COMPACTION_MAX_BILLED_ATTEMPTS); SDK retries on top made each
            # billed attempt three.
            return AnthropicClient(
                api_key=settings.anthropic_api_key,
                model=settings.proposal_assist_llm_model,
                max_retries=0,
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
            # Deferred: importing the proposal-assist stack at module scope made a match
            # process load pycrdt and prosemirror at boot for jobs it never runs.
            # Pinned by tests/architecture/test_match_edition_process_cut.py.
            from grant_lab.proposal_assist.analysis.pipeline import drain_analysis_queue

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
            # Deferred: importing the proposal-assist stack at module scope made a match
            # process load pycrdt and prosemirror at boot for jobs it never runs.
            # Pinned by tests/architecture/test_match_edition_process_cut.py.
            from grant_lab.proposal_assist.analysis.reconcile import reconcile_analysis_coverage

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
    if not scheduler.running:
        # A standby's scheduler is built but never started; modify() on it is a
        # silent no-op, and this used to report triggered=True for nothing
        # (review 2026-09-02 §2.6(b), prior #25).
        return False
    job = scheduler.get_job(job_id)
    if job is None:
        return False
    job.modify(next_run_time=_dt.now(UTC))
    return True
