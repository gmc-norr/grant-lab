from unittest.mock import MagicMock

import pytest

from grant_lab.email.client import EmailTransportError, SMTPClient


def test_smtp_success_returns_ok(monkeypatch):
    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    fake_conn.sendmail.return_value = {}  # dict of refused recipients; empty = success
    monkeypatch.setattr("grant_lab.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user="u", password="p", from_addr="bot@umu.se"
    )
    r = client.send(to="u@umu.se", subject="hi", html="<p>hi</p>", text="hi")
    assert r.ok
    assert r.bounce_reason is None
    fake_conn.starttls.assert_called_once()
    fake_conn.login.assert_called_once_with("u", "p")
    fake_conn.sendmail.assert_called_once()


def test_smtp_recipient_refused_is_bounce(monkeypatch):
    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    # smtplib raises SMTPRecipientsRefused for per-address rejects
    import smtplib

    fake_conn.sendmail.side_effect = smtplib.SMTPRecipientsRefused(
        {"u@umu.se": (550, b"5.1.1 user unknown")}
    )
    monkeypatch.setattr("grant_lab.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user="u", password="p", from_addr="bot@umu.se"
    )
    r = client.send(to="u@umu.se", subject="hi", html="<p>hi</p>", text="hi")
    assert not r.ok
    assert r.bounce_reason and "550" in r.bounce_reason


def test_smtp_connection_error_raises_transport(monkeypatch):
    import smtplib

    fake_smtp_class = MagicMock(side_effect=smtplib.SMTPConnectError(421, "down"))
    monkeypatch.setattr("grant_lab.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user="u", password="p", from_addr="bot@umu.se"
    )
    with pytest.raises(EmailTransportError):
        client.send(to="u@umu.se", subject="hi", html="<p>hi</p>", text="hi")


def test_smtp_no_login_when_no_user(monkeypatch):
    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    fake_conn.sendmail.return_value = {}
    monkeypatch.setattr("grant_lab.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user=None, password=None, from_addr="bot@umu.se"
    )
    r = client.send(to="u@umu.se", subject="hi", html="<p></p>", text="")
    assert r.ok
    fake_conn.login.assert_not_called()


def test_smtp_starttls_uses_verifying_context(monkeypatch):
    """STARTTLS must use an explicit SSL context that verifies the server cert.
    A bare conn.starttls() uses Python's CERT_NONE default (no cert/hostname
    verification), which lets an active MITM read the mail and the SMTP
    credentials. One implementation now carries the digest AND the admin
    alerts (auth/email.TransportEmailSender), so this covers both.
    """
    import ssl

    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    monkeypatch.setattr("grant_lab.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user="u", password="p", from_addr="bot@umu.se"
    )
    client.send(to="u@umu.se", subject="hi", html=None, text="link")

    fake_conn.starttls.assert_called_once()
    _, kwargs = fake_conn.starttls.call_args
    ctx = kwargs.get("context")
    assert isinstance(ctx, ssl.SSLContext)
    # A default context verifies the cert chain and the hostname.
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    fake_conn.login.assert_called_once_with("u", "p")


def test_smtp_without_html_sends_text_only(monkeypatch):
    """Alert mail passes html=None: one text/plain part, no HTML alternative."""
    import email

    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    fake_conn.sendmail.return_value = {}
    monkeypatch.setattr("grant_lab.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user="u", password="p", from_addr="bot@umu.se"
    )
    assert client.send(to="u@umu.se", subject="hi", html=None, text="plain").ok
    (_, _, raw), _ = fake_conn.sendmail.call_args
    sent = email.message_from_string(raw)
    assert not sent.is_multipart()
    assert sent.get_content_type() == "text/plain"
