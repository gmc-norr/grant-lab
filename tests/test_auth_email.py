import email
import json
import logging
import smtplib
from unittest.mock import MagicMock

import httpx
import pytest

from grant_lab.auth.email import (
    ConsoleEmailSender,
    EmailSendFailed,
    InMemoryEmailSender,
    TransportEmailSender,
)
from grant_lab.email.client import ResendClient, SMTPClient


def test_in_memory_sender_records_messages():
    sender = InMemoryEmailSender()
    sender.send(
        to="alice@umu.se",
        subject="Your login link",
        text_body="Click: https://x/verify?token=abc",
    )
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert msg.to == "alice@umu.se"
    assert msg.subject == "Your login link"
    assert "https://x/verify?token=abc" in msg.text_body


def test_console_sender_logs_full_body(caplog):
    sender = ConsoleEmailSender()
    with caplog.at_level(logging.INFO, logger="grant_lab.auth.email"):
        sender.send(
            to="alice@umu.se",
            subject="Your login link",
            text_body="Click: https://x/verify?token=abc",
        )
    assert any(
        "https://x/verify?token=abc" in r.getMessage()
        or "https://x/verify?token=abc" in str(getattr(r, "body", ""))
        for r in caplog.records
    )


# --- alert mail goes through the digest's delivery clients --------------------
# Review 2026-10-31 (lean-code item 2): auth/email.py used to carry its own
# Resend and SMTP code next to email/client.py's. Alerts now wrap the shared
# client; these tests pin what the wrapper must keep: plain text only, and ANY
# failure -- a bounce included -- raised as EmailSendFailed, because the alerter
# marks an alert sent only after a send succeeds.


def _resend_alerts(handler) -> TransportEmailSender:
    return TransportEmailSender(
        ResendClient(
            api_key="re_secret",
            from_addr="no-reply@umu.se",
            transport=httpx.MockTransport(handler),
        )
    )


def test_resend_alert_posts_a_text_only_body_and_auth_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "resend_123"})

    _resend_alerts(handler).send(to="alice@umu.se", subject="Hi", text_body="body")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["auth"] == "Bearer re_secret"
    assert captured["body"] == {
        "from": "no-reply@umu.se",
        "to": ["alice@umu.se"],
        "subject": "Hi",
        "text": "body",
    }


@pytest.mark.parametrize(
    "respond",
    [
        pytest.param(lambda: httpx.Response(502, text="bad gateway"), id="5xx"),
        pytest.param(
            lambda: httpx.Response(422, json={"name": "invalid_email", "message": "bad to"}),
            id="bounce",
        ),
    ],
)
def test_resend_alert_failure_raises_email_send_failed(respond):
    sender = _resend_alerts(lambda _request: respond())
    with pytest.raises(EmailSendFailed):
        sender.send(to="alice@umu.se", subject="Hi", text_body="body")


def test_resend_alert_network_error_raises_email_send_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    with pytest.raises(EmailSendFailed):
        _resend_alerts(handler).send(to="alice@umu.se", subject="Hi", text_body="body")


def _fake_smtp(monkeypatch):
    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    fake_conn.sendmail.return_value = {}
    monkeypatch.setattr("grant_lab.email.client.smtplib.SMTP", fake_smtp_class)
    return fake_smtp_class, fake_conn


def _smtp_alerts() -> TransportEmailSender:
    return TransportEmailSender(
        SMTPClient(
            host="smtp.umu.se",
            port=587,
            user="user@umu.se",
            password="secret",
            from_addr="no-reply@umu.se",
        )
    )


def test_smtp_alert_sends_plain_text_via_starttls_and_login(monkeypatch):
    _, fake_conn = _fake_smtp(monkeypatch)

    _smtp_alerts().send(to="alice@umu.se", subject="Source failing", text_body="3 failures")

    fake_conn.starttls.assert_called_once()
    fake_conn.login.assert_called_once_with("user@umu.se", "secret")
    (from_addr, to_addrs, raw), _ = fake_conn.sendmail.call_args
    assert (from_addr, to_addrs) == ("no-reply@umu.se", ["alice@umu.se"])
    sent = email.message_from_string(raw)
    assert not sent.is_multipart()
    assert sent.get_content_type() == "text/plain"
    assert "3 failures" in sent.get_payload()


def test_smtp_alert_connection_error_raises_email_send_failed(monkeypatch):
    fake_smtp_class = MagicMock(side_effect=smtplib.SMTPConnectError(421, "down"))
    monkeypatch.setattr("grant_lab.email.client.smtplib.SMTP", fake_smtp_class)

    with pytest.raises(EmailSendFailed):
        _smtp_alerts().send(to="alice@umu.se", subject="Hi", text_body="Click me")


def test_smtp_alert_refused_recipient_raises_email_send_failed(monkeypatch):
    _, fake_conn = _fake_smtp(monkeypatch)
    fake_conn.sendmail.side_effect = smtplib.SMTPRecipientsRefused(
        {"alice@umu.se": (550, b"no such user")}
    )

    with pytest.raises(EmailSendFailed):
        _smtp_alerts().send(to="alice@umu.se", subject="Hi", text_body="Click me")
