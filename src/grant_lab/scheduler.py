from __future__ import annotations

import contextlib
import logging
import threading
from datetime import UTC
from datetime import datetime as _dt
from typing import Protocol

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
from grant_lab.ranking.ollama_client import OllamaClient
from grant_lab.ranking.queue import reconcile_missing_rankings
from grant_lab.ranking.worker import drain_queue
from grant_lab.sources.registry import SOURCES

_logger = logging.getLogger(__name__)

# APScheduler's default thread pool is 10 workers. Some ticks hold a worker for
# minutes (the rank drain, up to RANKING_TICK_BUDGET_SECONDS; a per-source
# ingest). The pool is sized well above the job count so a slow tick never
# delays the crons that must run on time — daily ingest, the embedding
# backfill, the weekly digest.
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
        # AUTOCOMMIT, because this connection is held for the PROCESS LIFETIME.
        # With SQLAlchemy's default autobegin, `pg_try_advisory_lock` opens a
        # transaction that nothing ever ends: review 2026-10-26 found the live
        # lock backend `idle in transaction` with `xact_start` 20 days earlier.
        # Two concrete harms, not just untidiness. (1) Setting
        # `idle_in_transaction_session_timeout`, standard Postgres hardening,
        # kills exactly this session — dropping the lock, stopping the
        # scheduler and turning /healthz 503. (2) Any statement error on the
        # connection leaves the transaction ABORTED, after which `is_held()`'s
        # ping can only raise and the watchdog stops the scheduler on a lie
        # while Postgres still holds the lock.
        #
        # This does NOT weaken the lock: `pg_advisory_lock` is SESSION-scoped
        # and survives commit (only `pg_advisory_xact_lock` is released at
        # commit), so the lock still lives exactly as long as this connection.
        # Pinned by tests/test_scheduler_lock_autocommit.py, which checks from
        # a second connection that the lock is still held after several
        # autocommitted statements.
        #
        # Two steps, not one chained expression: if `execution_options` raised,
        # a chained call would leave the Connection `connect()` returned
        # unreferenced, so the handler below would return "unavailable" without
        # closing it — leaking one of the pool's connections AND starting the
        # app with single-instance enforcement silently off. It cannot raise
        # today, but this is the one code path where "silently off" is the
        # expensive outcome.
        opened = engine.connect()
        try:
            conn = opened.execution_options(isolation_level="AUTOCOMMIT")
        except Exception:
            opened.close()
            raise
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
    must keep working even when every executor worker is busy.

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
        anthropic_client = AnthropicClient(
            api_key=anthropic_api_key,
            model=remote_rank_model,
            effort=settings.remote_rank_effort,
            # The DRAIN TICK is the retry: a failed escalation leaves the local
            # score, and the pair comes back on a later tick bounded by
            # RANKING_REMOTE_DAILY_CAP. SDK retries on top made one read timeout
            # up to three billed Sonnet requests behind a single
            # ranking_remote_attempts row, so the cap under-counted by 3x
            # (review 2026-10-26).
            max_retries=0,
        )

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
