"""Tests for create_app() email-sender startup selection logic."""
import pytest

from call_monitor.auth.email import ConsoleEmailSender, ResendEmailSender, SMTPEmailSender
from call_monitor.main import create_app


def _set_min_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")


def test_resend_api_key_selects_resend_email_sender(monkeypatch, pg_url):
    """RESEND_API_KEY set → ResendEmailSender regardless of http/https."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "https://app.ki.se")
    monkeypatch.setenv("RESEND_API_KEY", "re_secret")
    app = create_app()
    assert isinstance(app.state.email_sender, ResendEmailSender)
    app.state.engine.dispose()


def test_smtp_host_without_resend_selects_smtp_email_sender(monkeypatch, pg_url):
    """SMTP_HOST set (no RESEND key) → SMTPEmailSender — accepted production transport."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "https://app.ki.se")
    monkeypatch.setenv("SMTP_HOST", "smtp.ki.se")
    monkeypatch.setenv("SMTP_USER", "user@ki.se")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    app = create_app()
    assert isinstance(app.state.email_sender, SMTPEmailSender)
    app.state.engine.dispose()


def test_no_email_creds_http_selects_console_email_sender(monkeypatch, pg_url):
    """No RESEND key, no SMTP, plain HTTP → ConsoleEmailSender (dev mode)."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    app = create_app()
    assert isinstance(app.state.email_sender, ConsoleEmailSender)
    app.state.engine.dispose()


def test_no_email_creds_https_raises_runtime_error(monkeypatch, pg_url):
    """No RESEND key, no SMTP, https → RuntimeError (refuse to log magic links)."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "https://app.ki.se")
    with pytest.raises(RuntimeError, match="RESEND_API_KEY or SMTP_HOST"):
        create_app()
