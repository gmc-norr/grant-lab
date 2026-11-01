"""Tests for create_app() email-sender startup selection logic."""

from grant_lab.auth.email import (
    ConsoleEmailSender,
    NoOpEmailSender,
    TransportEmailSender,
)
from grant_lab.email.client import ResendClient, SMTPClient
from grant_lab.main import create_app


def _set_min_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    # These tests use an https APP_BASE_URL, which now requires a strong
    # (>=32 char, non-placeholder) SESSION_SECRET — see config validator.
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    # https requires a real EMAIL_FROM (example.com placeholder is rejected in prod).
    monkeypatch.setenv("EMAIL_FROM", "noreply@umu.se")


def test_resend_api_key_selects_resend_email_sender(monkeypatch, pg_url):
    """RESEND_API_KEY set → alerts go through ResendClient, regardless of http/https."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "https://app.umu.se")
    monkeypatch.setenv("RESEND_API_KEY", "re_secret")
    app = create_app()
    assert isinstance(app.state.email_sender, TransportEmailSender)
    assert isinstance(app.state.email_sender.client, ResendClient)
    app.state.engine.dispose()


def test_smtp_host_without_resend_selects_smtp_email_sender(monkeypatch, pg_url):
    """SMTP_HOST set (no RESEND key) → alerts go through SMTPClient (a production transport)."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "https://app.umu.se")
    monkeypatch.setenv("SMTP_HOST", "smtp.umu.se")
    monkeypatch.setenv("SMTP_USER", "user@umu.se")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    app = create_app()
    assert isinstance(app.state.email_sender, TransportEmailSender)
    assert isinstance(app.state.email_sender.client, SMTPClient)
    app.state.engine.dispose()


def test_no_email_creds_http_selects_console_email_sender(monkeypatch, pg_url):
    """No RESEND key, no SMTP, plain HTTP → ConsoleEmailSender (dev mode)."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    app = create_app()
    assert isinstance(app.state.email_sender, ConsoleEmailSender)
    app.state.engine.dispose()


def test_no_email_creds_https_selects_noop_email_sender(monkeypatch, pg_url):
    """No RESEND key, no SMTP, https → NoOpEmailSender. The email sender is now
    best-effort only (magic-link login was removed), so prod no longer refuses to
    start; it drops alerts silently instead of console-logging their bodies."""
    _set_min_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "https://app.umu.se")
    app = create_app()
    assert isinstance(app.state.email_sender, NoOpEmailSender)
    app.state.engine.dispose()


def test_create_app_exposes_citation_service(monkeypatch, pg_url):
    """create_app() must set app.state.citation_service to a CitationLLMService
    instance so the upload/fetch routes can pass it to ingest_bytes."""
    from grant_lab.proposal_assist.citations import CitationLLMService

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
