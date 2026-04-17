import pytest

from call_monitor.config import Settings


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

    monkeypatch.setenv("APP_BASE_URL", "https://call-monitor.example.com")
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
