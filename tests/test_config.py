import pytest

from call_manager.config import Settings


def test_settings_parse_domain_list_from_csv(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se, karolinska.se,REGION.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    s = Settings()
    assert s.allowed_email_domains == ["ki.se", "karolinska.se", "region.se"]


def test_cookie_secure_follows_app_base_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")

    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    assert Settings().cookie_secure is False

    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")
    assert Settings().cookie_secure is True


def test_settings_parse_initial_admins_csv(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "a@ki.se, B@Ki.se")

    s = Settings()
    assert s.initial_admin_emails == ["a@ki.se", "b@ki.se"]


def test_settings_require_session_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    with pytest.raises(ValueError):
        Settings()


def test_create_app_refuses_to_start_without_resend_key_in_prod(monkeypatch, pg_url):
    """Security-critical invariant: in production (https URL) with no
    RESEND_API_KEY set, the app MUST refuse to boot rather than silently
    falling back to ConsoleEmailSender (which would log magic links to
    stderr — a 15-minute takeover window for anyone with log access).
    """
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        create_app()


def test_admin_dashboard_url_gets_trailing_slash_appended(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("ADMIN_DASHBOARD_URL", "https://app.example.com")

    s = Settings()
    assert s.admin_dashboard_url == "https://app.example.com/"


def test_admin_dashboard_url_already_trailing_slash_is_idempotent(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("ADMIN_DASHBOARD_URL", "https://app.example.com/")

    s = Settings()
    assert s.admin_dashboard_url == "https://app.example.com/"


def test_create_app_allows_console_sender_in_dev_without_resend_key(monkeypatch, pg_url):
    """Converse of the refuse-to-start test: plain HTTP APP_BASE_URL is
    dev, so ConsoleEmailSender is the intended fallback and create_app
    must NOT raise."""
    from call_manager.auth.email import ConsoleEmailSender
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    app = create_app()
    try:
        assert isinstance(app.state.email_sender, ConsoleEmailSender)
    finally:
        app.state.engine.dispose()
