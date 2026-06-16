import pytest
from fastapi.testclient import TestClient

from call_manager.config import Settings
from call_manager.main import create_app


def test_spa_mode_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("SPA_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "disabled"


def test_spa_mode_from_env(monkeypatch):
    monkeypatch.setenv("SPA_MODE", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "prod"


def _env(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        **extra,
    }.items():
        monkeypatch.setenv(k, v)


def test_disabled_mode_serves_no_spa(monkeypatch, pg_url, pg_session_factory):
    _env(monkeypatch, pg_url, SPA_MODE="disabled")
    c = TestClient(create_app())
    assert c.get("/app").status_code == 404


def test_prod_mode_missing_build_fails_fast(monkeypatch, pg_url, pg_session_factory, tmp_path):
    import call_manager.web.spa_serving as spa_serving

    monkeypatch.setattr(spa_serving, "SPA_DIR", tmp_path / "spa")
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    with pytest.raises(RuntimeError, match="SPA build missing"):
        create_app()


def test_prod_mode_serves_index_for_spa_routes(monkeypatch, pg_url, pg_session_factory, tmp_path):
    import call_manager.web.spa_serving as spa_serving

    spa_dir = tmp_path / "spa"
    (spa_dir / "assets").mkdir(parents=True)
    (spa_dir / "index.html").write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    c = TestClient(create_app())
    assert "<title>spa</title>" in c.get("/app").text
    assert c.get("/api/does-not-exist").status_code == 404
    assert c.get("/totally-unknown").status_code == 404
