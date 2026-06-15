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
    # Strong secret so the https branch passes the prod-secret validator.
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
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


def test_https_rejects_short_session_secret(monkeypatch):
    """In production (https) a <32-char SESSION_SECRET is refused."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "short-but-16-chars")  # 18 chars, < 32
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")

    with pytest.raises(ValueError, match="at least 32"):
        Settings()


def test_https_rejects_dev_placeholder_secret(monkeypatch):
    """A known dev placeholder secret is refused in production even if long."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "dev-only-session-secret-not-for-prod")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://call-manager.example.com")

    with pytest.raises(ValueError, match="placeholder"):
        Settings()


def test_http_allows_weak_secret_for_dev(monkeypatch):
    """Plain-HTTP dev is unaffected by the prod strong-secret rule."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    assert Settings().session_secret == "test-secret-at-least-16-chars"


def test_allowed_domains_normalized_to_punycode(monkeypatch):
    """A Unicode allowlist entry is stored in its punycode form so the login
    check (which also normalizes) compares like-for-like."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se, KAROLINSKA.SE")

    s = Settings()
    assert s.allowed_email_domains == ["ki.se", "karolinska.se"]


def test_settings_require_session_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")

    with pytest.raises(ValueError):
        Settings()


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


def test_citation_llm_provider_defaults_to_local(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    from call_manager.config import Settings

    assert Settings().citation_llm_provider == "ollama"


def test_reference_ingest_settings_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    from call_manager.config import Settings

    s = Settings()
    assert s.reference_ingest_concurrency == 3
    assert s.reference_max_batch_files == 25
    assert s.reference_max_batch_bytes == 200 * 1024 * 1024
