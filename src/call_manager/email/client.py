from __future__ import annotations

import re
import smtplib
import ssl
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
# Resend "name" values that are UNCONDITIONALLY per-recipient bounces.
# "invalid_email" is reliably address-level.
# NOTE: "missing_field" is excluded (sender-side payload regression).
# "validation_error" is NOT in this set because Resend reuses it for BOTH
# sender-side faults (unverified/malformed "from" domain, bad field shapes)
# AND recipient-address faults. Counting every validation_error as a bounce
# would auto-disable every recipient after 3 weekly cycles for OUR config bug.
# It is disambiguated by message inspection in send() (see _is_recipient_validation).
_RESEND_BOUNCE_NAMES = {
    "invalid_email",
}

# Compiled word-boundary patterns for sender-side hints. Using \b avoids
# misclassifying "from" in "Message from recipient" as a sender fault.
# The bare-substring approach would match "from" in any recipient error message
# that mentions the word "from" (e.g. "Rejected from …", "from user input").
_RESEND_SENDER_SIDE_PATTERNS = [
    re.compile(r"\bdomain\b", re.I),
    re.compile(r"\bapi[_\s]key\b", re.I),
    re.compile(r"\bverif", re.I),
    re.compile(r"\bfrom\b\s+(?:address|domain|email|field|header)", re.I),
]
# Substrings that mark a validation_error as a RECIPIENT-address fault.
# (`to` is backtick-quoted as Resend renders the field name; bare "to" would
# match far too many unrelated words.)
_RESEND_RECIPIENT_HINTS = ("`to`", "recipient", "email", "address")


def _is_recipient_validation(message: str) -> bool:
    """True when a Resend validation_error message indicates a bad RECIPIENT
    address (→ bounce) rather than a sender-side payload/config fault
    (→ transport error). Sender-side hints win ties: better to alert on our
    own config than to silently disable a real user."""
    if any(pat.search(message) for pat in _RESEND_SENDER_SIDE_PATTERNS):
        return False
    msg = message.lower()
    return any(h in msg for h in _RESEND_RECIPIENT_HINTS)


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
                raise EmailTransportError(f"resend 200 with non-JSON body: {e}") from e

        if 400 <= resp.status_code < 500:
            try:
                err = resp.json()
            except ValueError:
                err = {}
            name = str(err.get("name") or "")
            message = str(err.get("message") or "")
            is_bounce = name in _RESEND_BOUNCE_NAMES or (
                name == "validation_error" and _is_recipient_validation(message)
            )
            if is_bounce:
                return SendResult(
                    ok=False,
                    bounce_reason=f"{name}: {message}" if message else name,
                )
            # 4xx that ISN'T a per-address issue (auth_error, rate_limit_exceeded,
            # or a sender-side validation_error) → transport error (alert, don't count)
            raise EmailTransportError(f"resend {resp.status_code} {name}: {message}")

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
                # Verify the server cert + hostname. A bare starttls() uses
                # Python's CERT_NONE default, which silently accepts any cert
                # and leaves the digest contents + SMTP credentials open to an
                # active MITM. Mirrors the magic-link sender in auth/email.py.
                conn.starttls(context=ssl.create_default_context())
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
