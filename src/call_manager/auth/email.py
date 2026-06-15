from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as _EmailMessage
from typing import Protocol

import httpx

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
class SMTPEmailSender:
    """Send plain-text notification emails via stdlib smtplib (STARTTLS on 587).

    Mirrors the ResendEmailSender interface (`send(*, to, subject, text_body)`)
    so it can slot into the same email_sender slot in create_app(). Uses the
    same STARTTLS+login+sendmail flow as email.client.SMTPClient but targets the
    single-field text-only path — intentional duplication, v1. Since magic-link
    login was removed, this carries best-effort ingestion/digest alerts only.

    SECURITY: plain-text body only. The digest path uses email.client.SMTPClient
    which also supports HTML parts.
    """

    host: str
    port: int
    user: str | None
    password: str | None
    from_address: str
    timeout: float = 30.0

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        msg = _EmailMessage()
        msg["From"] = self.from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text_body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as conn:
                # Verify the server cert + hostname. A bare starttls() uses
                # Python's CERT_NONE default, which silently accepts any cert
                # and leaves message contents open to an active MITM.
                # create_default_context() enables CERT_REQUIRED and
                # check_hostname.
                ctx = ssl.create_default_context()
                conn.starttls(context=ctx)
                if self.user and self.password:
                    conn.login(self.user, self.password)
                conn.sendmail(self.from_address, [to], msg.as_string())
        except (smtplib.SMTPException, OSError) as e:
            raise EmailSendFailed(str(e)) from e


def _default_resend_client() -> httpx.Client:
    return httpx.Client(timeout=10.0)


@dataclass
class ResendEmailSender:
    """Send via Resend's HTTP API. Single long-lived httpx.Client —
    construct once in create_app(), reuse across requests.

    The `client` field is a DI seam: tests inject an httpx.MockTransport
    to assert request shape without hitting the network. Default
    constructs a 10-second-timeout client suitable for production use.
    """

    api_key: str
    from_address: str
    client: httpx.Client = field(default_factory=_default_resend_client)

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        try:
            r = self.client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "from": self.from_address,
                    "to": [to],
                    "subject": subject,
                    "text": text_body,
                },
            )
            r.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise EmailSendFailed(str(e)) from e
