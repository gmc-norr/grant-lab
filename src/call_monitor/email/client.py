from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import httpx


class EmailTransportError(Exception):
    """Network or transport-level failure. Caller should NOT count this
    as a bounce — it's batch-wide, not per-recipient."""


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    bounce_reason: str | None = None  # set when ok=False (synchronous bounce)


class EmailClient(Protocol):
    def send(self, *, to: str, subject: str, html: str, text: str) -> SendResult: ...


# Resend HTTP API codes that indicate a permanent ADDRESS-level failure.
# 4xx with one of these "name" values → bounce (don't retry, count it).
# Other 4xx (auth, rate-limit, server-side validation) → transport error
# (don't count, alert). Rate-limit is sender-scoped on Resend; treating it
# as a per-recipient bounce would auto-disable users for our quota issue.
_RESEND_BOUNCE_NAMES = {
    "validation_error",
    "invalid_email",
    "missing_field",
}


class ResendClient:
    """Resend HTTPS client. Auth via Bearer api_key. Posts JSON to /emails."""

    _BASE = "https://api.resend.com"

    def __init__(
        self,
        *,
        api_key: str,
        from_addr: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._api_key = api_key
        self._from = from_addr
        self._transport = transport
        self._timeout = timeout

    def send(self, *, to: str, subject: str, html: str, text: str) -> SendResult:
        try:
            with httpx.Client(
                base_url=self._BASE, transport=self._transport, timeout=self._timeout
            ) as client:
                resp = client.post(
                    "/emails",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "from": self._from,
                        "to": [to],
                        "subject": subject,
                        "html": html,
                        "text": text,
                    },
                )
        except httpx.HTTPError as e:
            raise EmailTransportError(f"resend network error: {e}") from e

        if resp.status_code == 200:
            try:
                data = resp.json()
                return SendResult(ok=True, message_id=str(data.get("id") or ""))
            except ValueError as e:
                raise EmailTransportError(
                    f"resend 200 with non-JSON body: {e}"
                ) from e

        if 400 <= resp.status_code < 500:
            try:
                err = resp.json()
            except ValueError:
                err = {}
            name = str(err.get("name") or "")
            message = str(err.get("message") or "")
            if name in _RESEND_BOUNCE_NAMES:
                return SendResult(
                    ok=False,
                    bounce_reason=f"{name}: {message}" if message else name,
                )
            # 4xx that ISN'T a per-address issue (e.g. auth_error, rate_limit_exceeded) → transport
            raise EmailTransportError(
                f"resend {resp.status_code} {name}: {message}"
            )

        # 5xx
        raise EmailTransportError(f"resend {resp.status_code}")


class SMTPClient:
    """Stdlib smtplib wrapper. STARTTLS on port 587 by convention.

    Treats per-address SMTPRecipientsRefused as bounces; other smtplib
    exceptions as transport errors."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str | None,
        password: str | None,
        from_addr: str,
        timeout: float = 30.0,
    ):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr
        self._timeout = timeout

    def send(self, *, to: str, subject: str, html: str, text: str) -> SendResult:
        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")

        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as conn:
                conn.starttls()
                if self._user and self._password:
                    conn.login(self._user, self._password)
                conn.sendmail(self._from, [to], msg.as_string())
        except smtplib.SMTPRecipientsRefused as e:
            # Per-recipient reject — bounce
            details = "; ".join(
                f"{addr}:{code} {msg.decode() if isinstance(msg, bytes) else msg}"
                for addr, (code, msg) in e.recipients.items()
            )
            return SendResult(ok=False, bounce_reason=f"refused: {details}")
        except (smtplib.SMTPException, OSError) as e:
            raise EmailTransportError(f"smtp error: {e}") from e

        return SendResult(ok=True, message_id=None)
