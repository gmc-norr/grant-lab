from dataclasses import dataclass

import pytest

from call_monitor.email.client import ResendClient, SMTPClient
from call_monitor.email.factory import EmailConfigError, build_email_client


@dataclass
class _FakeSettings:
    """Settings-shaped object exposing only the fields build_email_client reads.

    The real Settings class can't be constructed with kwargs (uses Field(alias)
    + reads env vars), so tests pass a duck-typed stand-in."""
    email_transport: str = "resend"
    resend_api_key: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str = "bot@ki.se"


def test_factory_returns_resend_when_configured():
    s = _FakeSettings(email_transport="resend", resend_api_key="key")
    c = build_email_client(s)
    assert isinstance(c, ResendClient)


def test_factory_returns_smtp_when_configured():
    s = _FakeSettings(email_transport="smtp", smtp_host="smtp.example.com")
    c = build_email_client(s)
    assert isinstance(c, SMTPClient)


def test_factory_rejects_resend_without_key():
    s = _FakeSettings(email_transport="resend")
    with pytest.raises(EmailConfigError, match="RESEND_API_KEY"):
        build_email_client(s)


def test_factory_rejects_smtp_without_host():
    s = _FakeSettings(email_transport="smtp")
    with pytest.raises(EmailConfigError, match="SMTP_HOST"):
        build_email_client(s)


def test_factory_rejects_unknown_transport():
    s = _FakeSettings(email_transport="carrier-pigeon")
    with pytest.raises(EmailConfigError, match="unknown"):
        build_email_client(s)
