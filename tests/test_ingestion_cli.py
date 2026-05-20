"""Tests for the ingestion CLI: `python -m call_manager.ingestion.cli run --source <slug>`.

The CLI is a deploy-script entry point (per spec 2026-04-19 §9). Its exit
code is load-bearing — deploy chains on `&&`, so any silent zero on a real
failure would proceed past a broken ingest.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from call_manager.ingestion.cli import ingest_main


@pytest.fixture
def _settings_env(monkeypatch, pg_url):
    """Populate the env vars Settings() requires, scoped to one test.

    `_run_for_source` (and the un-patched `run` path) instantiate Settings()
    to read source base_urls + ollama config. Without these vars Settings
    would raise during pydantic validation, before our code under test runs.
    """
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def test_cli_invokes_orchestrator_for_named_source(
    pg_session_factory,
    _settings_env,
):
    """`run --source vinnova` calls the orchestrator with the Vinnova adapter."""
    fake_run = MagicMock(return_value=None)
    with patch("call_manager.ingestion.cli._run_for_source", fake_run):
        exit_code = ingest_main(
            argv=["run", "--source", "vinnova"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 0
    fake_run.assert_called_once()
    args, kwargs = fake_run.call_args
    # Slug propagates
    assert kwargs.get("slug") == "vinnova" or "vinnova" in args


def test_cli_unknown_source_returns_nonzero(pg_session_factory, _settings_env):
    """`run --source garbage` exits 1 with a clear error."""
    exit_code = ingest_main(
        argv=["run", "--source", "garbage"],
        session_factory=pg_session_factory,
    )
    assert exit_code == 1


def test_cli_no_source_lists_available_sources(pg_session_factory, capsys):
    """`list-sources` prints registered sources."""
    exit_code = ingest_main(
        argv=["list-sources"],
        session_factory=pg_session_factory,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "vinnova" in captured.out
    assert "eu_portal" in captured.out


def test_cli_returns_nonzero_when_orchestrator_reports_error(
    pg_session_factory,
    capsys,
    _settings_env,
):
    """Codex H2 regression: when run_ingestion returns an IngestionResult
    with .error set (adapter HTTP failure, DB error during enqueue, etc.),
    the CLI must exit 1 — deploy scripts chain on `&&` and would otherwise
    proceed past a real failure."""
    fake_failed_result = MagicMock(error="adapter timed out", inserted=0, updated=0)
    with patch(
        "call_manager.ingestion.cli._run_for_source",
        return_value=fake_failed_result,
    ):
        exit_code = ingest_main(
            argv=["run", "--source", "vinnova"],
            session_factory=pg_session_factory,
        )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "adapter timed out" in captured.err
