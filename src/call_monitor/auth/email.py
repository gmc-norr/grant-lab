from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

_logger = logging.getLogger(__name__)


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


@dataclass
class ResendEmailSender:
    api_key: str
    from_address: str
    _client: httpx.Client | None = None

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        client = self._client or httpx.Client(timeout=10.0)
        try:
            r = client.post(
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
        finally:
            if self._client is None:
                client.close()
