from unittest.mock import MagicMock

import pytest

from call_manager.email.client import EmailTransportError, SMTPClient


def test_smtp_success_returns_ok(monkeypatch):
    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    fake_conn.sendmail.return_value = {}  # dict of refused recipients; empty = success
    monkeypatch.setattr("call_manager.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user="u", password="p", from_addr="bot@ki.se"
    )
    r = client.send(to="u@ki.se", subject="hi", html="<p>hi</p>", text="hi")
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
        {"u@ki.se": (550, b"5.1.1 user unknown")}
    )
    monkeypatch.setattr("call_manager.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user="u", password="p", from_addr="bot@ki.se"
    )
    r = client.send(to="u@ki.se", subject="hi", html="<p>hi</p>", text="hi")
    assert not r.ok
    assert r.bounce_reason and "550" in r.bounce_reason


def test_smtp_connection_error_raises_transport(monkeypatch):
    import smtplib

    fake_smtp_class = MagicMock(side_effect=smtplib.SMTPConnectError(421, "down"))
    monkeypatch.setattr("call_manager.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user="u", password="p", from_addr="bot@ki.se"
    )
    with pytest.raises(EmailTransportError):
        client.send(to="u@ki.se", subject="hi", html="<p>hi</p>", text="hi")


def test_smtp_no_login_when_no_user(monkeypatch):
    fake_smtp_class = MagicMock()
    fake_conn = MagicMock()
    fake_smtp_class.return_value.__enter__.return_value = fake_conn
    fake_conn.sendmail.return_value = {}
    monkeypatch.setattr("call_manager.email.client.smtplib.SMTP", fake_smtp_class)

    client = SMTPClient(
        host="smtp.example.com", port=587, user=None, password=None, from_addr="bot@ki.se"
    )
    r = client.send(to="u@ki.se", subject="hi", html="<p></p>", text="")
    assert r.ok
    fake_conn.login.assert_not_called()
