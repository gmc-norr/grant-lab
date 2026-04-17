from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
    """Local-dev sender: writes the full message to the logger so developers
    can copy the magic link out of the app's stderr stream.

    SECURITY: never select this in production — it logs the full magic link
    body, which inside a 15-minute window is equivalent to a session token.
    `create_app()` only allows it when `cookie_secure=False` (i.e. dev over
    plain HTTP); a misconfigured prod (https without `RESEND_API_KEY`) must
    fail to start, not silently log credentials.
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
