# tests/test_config_google_oidc.py
import pytest

from call_manager.config import Settings


def _base(monkeypatch, **extra):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://app.ki.se")
    # Real EMAIL_FROM required in prod (https); example.com default is rejected.
    monkeypatch.setenv("EMAIL_FROM", "noreply@ki.se")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_oidc_disabled_by_default(monkeypatch):
    s = _base(monkeypatch)
    assert s.google_oidc_enabled is False
    assert s.google_redirect_uri == "https://app.ki.se/auth/google/callback"


def test_enabled_requires_client_credentials(monkeypatch):
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        _base(monkeypatch, GOOGLE_OIDC_ENABLED="true")


def test_enabled_with_credentials_ok(monkeypatch):
    s = _base(
        monkeypatch,
        GOOGLE_OIDC_ENABLED="true",
        GOOGLE_CLIENT_ID="cid",
        GOOGLE_CLIENT_SECRET="csecret",
    )
    assert s.google_oidc_enabled is True
    assert s.google_client_id == "cid"
