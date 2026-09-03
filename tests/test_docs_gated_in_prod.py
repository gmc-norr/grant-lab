"""Defect 1: /docs, /redoc, /openapi.json must be disabled outside spa_mode='dev'.

These routes are enabled by FastAPI's defaults. Enabling them in prod leaks
the full API surface unauthenticated. The fix passes docs_url=None etc. to
FastAPI() unless spa_mode is 'dev'.

We assert on the FastAPI app-level URL attributes (openapi_url, docs_url,
redoc_url) which are set at construction time — no lifespan / DB needed.
"""

from __future__ import annotations

import pytest

from call_manager.main import create_app


def _make_app(monkeypatch, *, spa_mode: str):
    # Use the real pg_url fixture only when we need the lifespan (we don't here).
    # A dummy URL is fine because create_app() builds the engine lazily.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("SPA_MODE", spa_mode)
    return create_app()


@pytest.mark.parametrize("spa_mode", ["disabled"])
def test_openapi_url_is_none_in_non_dev(monkeypatch, spa_mode):
    """FastAPI.openapi_url must be None so the route is never registered."""
    app = _make_app(monkeypatch, spa_mode=spa_mode)
    assert app.openapi_url is None


@pytest.mark.parametrize("spa_mode", ["disabled"])
def test_docs_url_is_none_in_non_dev(monkeypatch, spa_mode):
    app = _make_app(monkeypatch, spa_mode=spa_mode)
    assert app.docs_url is None


@pytest.mark.parametrize("spa_mode", ["disabled"])
def test_redoc_url_is_none_in_non_dev(monkeypatch, spa_mode):
    app = _make_app(monkeypatch, spa_mode=spa_mode)
    assert app.redoc_url is None


def test_openapi_url_is_set_in_dev(monkeypatch):
    """In spa_mode=dev the schema URL must be present for local DX / codegen."""
    app = _make_app(monkeypatch, spa_mode="dev")
    assert app.openapi_url == "/openapi.json"
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
