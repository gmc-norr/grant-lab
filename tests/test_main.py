"""Tests for create_app() email-sender startup selection logic."""

import pytest

from call_manager.auth.email import ConsoleEmailSender, ResendEmailSender, SMTPEmailSender
from call_manager.main import create_app


def _set_min_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    # These tests use an https APP_BASE_URL, which now requires a strong
    # (>=32 char, non-placeholder) SESSION_SECRET — see config validator.
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
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


def test_create_app_exposes_citation_service(monkeypatch, pg_url):
    """create_app() must set app.state.citation_service to a CitationLLMService
    instance so the upload/fetch routes can pass it to ingest_bytes."""
    from call_manager.proposal_assist.citations import CitationLLMService

    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    app = create_app()
    try:
        assert hasattr(app.state, "citation_service"), (
            "app.state.citation_service must be set by create_app()"
        )
        assert isinstance(app.state.citation_service, CitationLLMService), (
            f"expected CitationLLMService, got {type(app.state.citation_service)}"
        )
    finally:
        app.state.engine.dispose()
