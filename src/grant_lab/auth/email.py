from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from grant_lab.email.client import EmailClient, EmailTransportError

_logger = logging.getLogger(__name__)


class EmailSendFailed(Exception):
    """Raised by a sender when the upstream service fails to accept the
    message. Callers (e.g. /login) can choose to log-and-continue rather
    than surfacing an internal error that leaks whether the address was
    worth sending to."""


@dataclass
class EmailMessage:
    to: str
    subject: str
    text_body: str


class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, text_body: str) -> None: ...


@dataclass
class InMemoryEmailSender:
    """Pure test double — appends to a list, nothing else."""

    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        self.sent.append(EmailMessage(to=to, subject=subject, text_body=text_body))


class ConsoleEmailSender:
    """Local-dev sender: writes the full message (subject + body) to the logger
    so developers can read outgoing alert/digest mail from the app's stderr.

    SECURITY: never select this in production — it logs the full message body to
    the log aggregator. `create_app()` only allows it when `cookie_secure=False`
    (i.e. dev over plain HTTP); production falls back to NoOpEmailSender instead,
    which drops messages without logging their contents.
    """

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        _logger.info(
            "email_sent (console — development)",
            extra={
                "event": "email_sent",
                "to": to,
                "subject": subject,
                "body": text_body,
            },
        )


@dataclass
class NoOpEmailSender:
    """Drops messages without delivering OR logging their contents. Production
    fallback when no transport is configured: best-effort ingestion alerts are
    discarded rather than console-logged (alert bodies include error details)."""

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        _logger.info("email dropped (no transport configured)", extra={"event": "email_dropped"})


@dataclass
class TransportEmailSender:
    """Plain-text admin alerts over the shared delivery client.

    `client` is the same Resend/SMTP implementation the weekly digest uses
    (email/client.py, built by email.factory.build_email_client), so TLS
    verification, auth and error handling live in one place. This file used to
    carry a second copy of both transports (review 2026-10-31, lean-code item 2).

    Plain text only: `html=None` sends no HTML part. ANY failure raises
    EmailSendFailed -- a synchronous bounce included. The digest counts a
    bounce per recipient; the alerter must instead treat it as "not sent", since
    it marks an alert sent only after a send succeeds (ingestion/alerts.py).
    """

    client: EmailClient

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        try:
            result = self.client.send(to=to, subject=subject, html=None, text=text_body)
        except EmailTransportError as e:
            raise EmailSendFailed(str(e)) from e
        if not result.ok:
            raise EmailSendFailed(result.bounce_reason or "recipient rejected")
