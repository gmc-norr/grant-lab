"""Tests for the source-adapter registry.

Verifies the single source of truth for which funding sources exist —
adding a new adapter requires only an entry in `SOURCES` (plus the adapter
file), and the scheduler + ingestion CLI both pick it up automatically.
"""
from __future__ import annotations

import pytest

from call_monitor.config import Settings
from call_monitor.sources.eu_portal import EUPortalAdapter
from call_monitor.sources.registry import (
    SOURCES,
    build_adapter,
    known_slugs,
)
from call_monitor.sources.vinnova import VinnovaAdapter


@pytest.fixture
def _settings_env(monkeypatch):
    """Minimum env vars Settings() requires for instantiation."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def test_known_slugs_returns_all_registered_sources():
    """Every entry in SOURCES is returned by known_slugs(), in order."""
    slugs = known_slugs()
    assert set(slugs) >= {"vinnova", "eu_portal"}
    # Order must match SOURCES (list-sources output stability).
    assert slugs == [cfg.slug for cfg in SOURCES]


def test_build_adapter_returns_none_for_unknown_slug(_settings_env):
    settings = Settings()
    assert build_adapter("nonexistent_source", settings) is None


def test_build_adapter_returns_correct_adapter_class_for_each_known_slug(
    _settings_env,
):
    """Each registered slug builds an instance of its expected adapter class.

    This exercises the lambda in each SourceConfig.build, catching shape
    mismatches between the lambda body and the Settings fields it reads.
    """
    settings = Settings()
    expected = {
        "eu_portal": EUPortalAdapter,
        "vinnova": VinnovaAdapter,
    }
    for slug, cls in expected.items():
        adapter = build_adapter(slug, settings)
        assert adapter is not None, f"registry missing slug {slug!r}"
        assert isinstance(adapter, cls), (
            f"build_adapter({slug!r}) returned {type(adapter).__name__}, "
            f"expected {cls.__name__}"
        )
