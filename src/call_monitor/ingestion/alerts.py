from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass

from call_monitor.auth.email import EmailSender, EmailSendFailed

_logger = logging.getLogger(__name__)


@dataclass
class _SourceState:
    consecutive_failures: int = 0
    alerted: bool = False
    last_error: str | None = None


class ConsecutiveFailureTracker:
    """Per-source streak counter for ingestion failures.

    Single-instance assumption (state is in-memory). On reaching the threshold
    on a given source, sends one admin email; further failures on the same
    streak do not re-alert. A single success resets the counter and re-arms
    alerting for the next streak.
    """

    def __init__(
        self,
        *,
        threshold: int,
        sender: EmailSender,
        admin_email: str | None,
    ):
        self._threshold = threshold
        self._sender = sender
        self._admin_email = admin_email
        self._state: dict[str, _SourceState] = defaultdict(_SourceState)
        self._lock = threading.Lock()

    def record_failure(self, source_slug: str, *, error: str | None = None) -> None:
        with self._lock:
            st = self._state[source_slug]
            st.consecutive_failures += 1
            st.last_error = error
            should_attempt_alert = (
                st.consecutive_failures >= self._threshold
                and not st.alerted
                and self._admin_email is not None
            )
        if not should_attempt_alert:
            return

        # CRITICAL: only mark `alerted=True` AFTER the send succeeds. If we
        # flipped it pre-send (the obvious mistake) and the email provider
        # is also down, we'd suppress every future alert for this streak —
        # one transient mail outage masks the real ongoing ingestion outage
        # the alert was supposed to surface.
        try:
            subject = (
                f"[call-monitor] {source_slug} failing: "
                f"{self._threshold}+ consecutive errors"
            )
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
            return

        with self._lock:
            self._state[source_slug].alerted = True

    def record_success(self, source_slug: str) -> None:
        with self._lock:
            st = self._state[source_slug]
            st.consecutive_failures = 0
            st.alerted = False
            st.last_error = None
