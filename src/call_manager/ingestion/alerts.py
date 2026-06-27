from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from call_manager.auth.email import EmailSender, EmailSendFailed

_logger = logging.getLogger(__name__)


@dataclass
class _SourceState:
    consecutive_failures: int = 0
    alerted: bool = False
    last_error: str | None = None


class ConsecutiveFailureTracker:
    """Per-source streak counter for ingestion failures.

    Persists the failure streak and last-alerted timestamp to the ``Source``
    row so the count survives process restarts and OOM kills.  An in-memory
    cache mirrors the DB state so the hot path (record_failure / record_success
    called after every ingest run) avoids an extra SELECT on the common case
    where a session_factory is not provided.

    On reaching the threshold on a given source, sends one admin email; further
    failures on the same streak do not re-alert (guarded by ``last_alerted_at``
    in the DB and ``alerted`` in the in-memory mirror).  A single success resets
    the counter and re-arms alerting for the next streak.

    Args:
        threshold: Number of consecutive failures before alerting.
        sender: Email sender implementation.
        admin_email: Admin address to alert, or None to disable alerts.
        session_factory: If provided, streak state is persisted to the Source
            row transactionally.  Pass None for tests that don't need DB
            persistence (existing in-memory-only tests remain unchanged).
    """

    def __init__(
        self,
        *,
        threshold: int,
        sender: EmailSender,
        admin_email: str | None,
        session_factory: sessionmaker | None = None,
    ):
        self._threshold = threshold
        self._sender = sender
        self._admin_email = admin_email
        self._session_factory = session_factory
        self._state: dict[str, _SourceState] = defaultdict(_SourceState)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_from_db(self, source_slug: str) -> None:
        """Warm the in-memory cache from the DB row if a session_factory is
        configured.  Idempotent — called lazily on first access per slug."""
        if self._session_factory is None:
            return
        from call_manager.db.models import Source  # local import avoids circular

        try:
            with self._session_factory() as session:
                src = session.execute(
                    select(Source).where(Source.slug == source_slug)
                ).scalar_one_or_none()
                if src is None:
                    return
                with self._lock:
                    st = self._state[source_slug]
                    st.consecutive_failures = src.consecutive_failures
                    # alerted mirrors whether last_alerted_at is set AND the
                    # streak count still meets the threshold; it will be flipped
                    # on success (reset) or on the next alert send.
                    st.alerted = (
                        src.last_alerted_at is not None
                        and src.consecutive_failures >= self._threshold
                    )
        except Exception:  # noqa: BLE001
            _logger.warning("alerter.db_load_failed", extra={"source": source_slug}, exc_info=True)

    def _persist_failure(self, source_slug: str, new_count: int, alert_sent: bool) -> None:
        """Persist failure streak (and optionally last_alerted_at) to the DB."""
        if self._session_factory is None:
            return
        from call_manager.db.models import Source  # local import avoids circular

        try:
            now = datetime.now(UTC)
            values: dict = {"consecutive_failures": new_count}
            if alert_sent:
                values["last_alerted_at"] = now
            with self._session_factory() as session:
                session.execute(
                    Source.__table__.update()
                    .where(Source.slug == source_slug)
                    .values(**values)
                )
                session.commit()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "alerter.db_persist_failed", extra={"source": source_slug}, exc_info=True
            )

    def _persist_reset(self, source_slug: str) -> None:
        """Reset the persisted streak to 0 on success."""
        if self._session_factory is None:
            return
        from call_manager.db.models import Source  # local import avoids circular

        try:
            with self._session_factory() as session:
                session.execute(
                    Source.__table__.update()
                    .where(Source.slug == source_slug)
                    .values(consecutive_failures=0)
                )
                session.commit()
        except Exception:  # noqa: BLE001
            _logger.warning(
                "alerter.db_reset_failed", extra={"source": source_slug}, exc_info=True
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def record_failure(self, source_slug: str, *, error: str | None = None) -> None:
        # Warm cache from DB on first encounter for this slug (noop if already
        # loaded or if session_factory is None).
        with self._lock:
            already_loaded = source_slug in self._state
        if not already_loaded:
            self._load_from_db(source_slug)

        with self._lock:
            st = self._state[source_slug]
            st.consecutive_failures += 1
            st.last_error = error
            new_count = st.consecutive_failures
            should_attempt_alert = (
                new_count >= self._threshold
                and not st.alerted
                and self._admin_email is not None
            )

        if not should_attempt_alert:
            # Still persist the incremented count even when not alerting.
            self._persist_failure(source_slug, new_count, alert_sent=False)
            return

        # CRITICAL: only mark `alerted=True` AFTER the send succeeds. If we
        # flipped it pre-send (the obvious mistake) and the email provider
        # is also down, we'd suppress every future alert for this streak —
        # one transient mail outage masks the real ongoing ingestion outage
        # the alert was supposed to surface.
        try:
            subject = f"[call-manager] {source_slug} failing: {self._threshold}+ consecutive errors"
            self._sender.send(
                to=self._admin_email,
                subject=subject,
                text_body=(
                    f"Source `{source_slug}` has failed {self._threshold} or more "
                    f"consecutive ingestion runs.\n\n"
                    f"Last error: {error or '(no detail)'}\n"
                ),
            )
        except EmailSendFailed as e:
            # Email provider outage shouldn't crash the orchestrator and
            # shouldn't flip alerted=True — the next ingestion failure will
            # retry the send. Real bugs (TypeError, etc.) still surface.
            _logger.warning(
                "alerter.send_failed",
                extra={"event": "alerter_send_failed", "exc": str(e)},
            )
            # Still persist the count so a restart doesn't lose the streak.
            self._persist_failure(source_slug, new_count, alert_sent=False)
            return

        with self._lock:
            self._state[source_slug].alerted = True

        self._persist_failure(source_slug, new_count, alert_sent=True)

    def record_success(self, source_slug: str) -> None:
        with self._lock:
            st = self._state[source_slug]
            st.consecutive_failures = 0
            st.alerted = False
            st.last_error = None

        self._persist_reset(source_slug)


def check_stale_sources(
    *,
    session_factory: sessionmaker,
    sender: EmailSender,
    admin_email: str | None,
    stale_days: int,
) -> None:
    """Alert if any source has not been successfully fetched in ``stale_days`` days.

    This is a restart-independent backstop: unlike the consecutive-failure
    streak (which can only count failures that occur while the process is
    running), this check fires purely from the persisted ``last_fetched_at``
    timestamp.  A source that has been silently skipped because the scheduler
    restarted before completing three consecutive failures within the window
    will still trigger this alert.

    The function sends a single consolidated email listing all stale sources.
    It is idempotent — callers can fire it once per day (e.g. from a daily
    cron after all ingest jobs have run) or at scheduler startup.
    """
    if admin_email is None:
        return

    from call_manager.db.models import Source  # local import avoids circular

    cutoff = datetime.now(UTC) - timedelta(days=stale_days)
    try:
        with session_factory() as session:
            stale = (
                session.execute(
                    select(Source).where(
                        (Source.last_fetched_at.is_(None))
                        | (Source.last_fetched_at < cutoff)
                    )
                )
                .scalars()
                .all()
            )
    except Exception:  # noqa: BLE001
        _logger.warning("alerter.stale_check_db_failed", exc_info=True)
        return

    if not stale:
        return

    lines = "\n".join(
        f"  • {s.slug}: last fetched "
        f"{s.last_fetched_at.isoformat() if s.last_fetched_at else 'NEVER'}"
        for s in stale
    )
    try:
        sender.send(
            to=admin_email,
            subject=f"[call-manager] {len(stale)} source(s) not fetched in {stale_days}+ days",
            text_body=(
                f"The following source(s) have not been successfully fetched "
                f"in the last {stale_days} days:\n\n{lines}\n\n"
                "Check the ingestion logs and confirm the scheduler is running.\n"
            ),
        )
    except EmailSendFailed as e:
        _logger.warning(
            "alerter.stale_send_failed",
            extra={"event": "alerter_stale_send_failed", "exc": str(e)},
        )
