import json
import logging
from unittest.mock import MagicMock

import httpx
import pytest

from call_monitor.auth.email import (
    ConsoleEmailSender,
    EmailSendFailed,
    InMemoryEmailSender,
    ResendEmailSender,
    SMTPEmailSender,
)


def test_in_memory_sender_records_messages():
    sender = InMemoryEmailSender()
    sender.send(
        to="alice@ki.se",
        subject="Your login link",
        text_body="Click: https://x/verify?token=abc",
    )
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert msg.to == "alice@ki.se"
    assert msg.subject == "Your login link"
    assert "https://x/verify?token=abc" in msg.text_body


def test_console_sender_logs_full_body(caplog):
    sender = ConsoleEmailSender()
    with caplog.at_level(logging.INFO, logger="call_monitor.auth.email"):
        sender.send(
            to="alice@ki.se",
            subject="Your login link",
            text_body="Click: https://x/verify?token=abc",
        )
    assert any(
        "https://x/verify?token=abc" in r.getMessage()
        or "https://x/verify?token=abc" in str(getattr(r, "body", ""))
        for r in caplog.records
    )


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def test_resend_sender_posts_expected_body_and_auth_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "resend_123"})

    client = httpx.Client(transport=_mock_transport(handler))
    sender = ResendEmailSender(
        api_key="re_secret", from_address="no-reply@ki.se", client=client
    )
    sender.send(to="alice@ki.se", subject="Hi", text_body="body")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["auth"] == "Bearer re_secret"
    assert captured["body"] == {
        "from": "no-reply@ki.se",
        "to": ["alice@ki.se"],
        "subject": "Hi",
        "text": "body",
    }


def test_resend_sender_raises_email_send_failed_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = httpx.Client(transport=_mock_transport(handler))
    sender = ResendEmailSender(
        api_key="re_secret", from_address="no-reply@ki.se", client=client
    )
    with pytest.raises(EmailSendFailed):
        sender.send(to="alice@ki.se", subject="Hi", text_body="body")


def test_resend_sender_raises_email_send_failed_on_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    client = httpx.Client(transport=_mock_transport(handler))
    sender = ResendEmailSender(
        api_key="re_secret", from_address="no-reply@ki.se", client=client
    )
    with pytest.raises(EmailSendFailed):
        sender.send(to="alice@ki.se", subject="Hi", text_body="body")


def test_resend_sender_default_client_has_10s_timeout():
    sender = ResendEmailSender(api_key="k", from_address="f@ki.se")
    try:
        assert sender.client.timeout.connect == 10.0
        assert sender.client.timeout.read == 10.0
    finally:
        sender.client.close()


def test_smtp_email_sender_sends_magic_link(monkeypatch):
    """SMTPEmailSender must send the magic-link via STARTTLS+login, matching
    the SMTPClient flow in email.client but through the auth.email interface."""
    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    fake_conn.sendmail.return_value = {}
    monkeypatch.setattr("call_monitor.auth.email.smtplib.SMTP", fake_smtp_class)

    sender = SMTPEmailSender(
        host="smtp.ki.se",
        port=587,
        user="user@ki.se",
        password="secret",
        from_address="no-reply@ki.se",
    )
    sender.send(to="alice@ki.se", subject="Your login link", text_body="Click: https://x/verify?t=abc")

    fake_conn.starttls.assert_called_once()
    fake_conn.login.assert_called_once_with("user@ki.se", "secret")
    fake_conn.sendmail.assert_called_once()
    # Verify the recipient and from address
    args = fake_conn.sendmail.call_args
    assert args[0][0] == "no-reply@ki.se"
    assert args[0][1] == ["alice@ki.se"]
    assert "https://x/verify?t=abc" in args[0][2]


def test_smtp_email_sender_raises_email_send_failed_on_smtp_error(monkeypatch):
    import smtplib
    fake_smtp_class = MagicMock(side_effect=smtplib.SMTPConnectError(421, "down"))
    monkeypatch.setattr("call_monitor.auth.email.smtplib.SMTP", fake_smtp_class)

    sender = SMTPEmailSender(
        host="smtp.ki.se", port=587, user="u", password="p", from_address="no-reply@ki.se"
    )
    with pytest.raises(EmailSendFailed):
        sender.send(to="alice@ki.se", subject="Hi", text_body="Click me")
