"""Review 2026-09-02 A6 F1: a hanging test is the worst CI signal there is.

`test_oversized_frame_closes_the_connection` detects the missing frame cap by
BLOCKING in `receive_bytes()`, not by failing. With the cap disarmed the review
measured 595 s of silence and no failing test id — the job was killed by the
runner's global timeout. Several collab tests share that blocking-receive shape.

`pytest-timeout` converts every one of them into a named failure. This file
pins the wiring, because the setting lives in `pyproject.toml` where nothing
else would notice its removal: measured with the cap disarmed, the same test
went from an unbounded hang to `FAILED … in 121.15s`.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pytest_options() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]


def test_pytest_timeout_is_installed():
    assert importlib.util.find_spec("pytest_timeout") is not None, (
        "pytest-timeout is a dev dependency; without it the `timeout` setting is inert"
    )


def test_a_suite_wide_timeout_is_configured():
    timeout = _pytest_options().get("timeout")
    assert timeout is not None, "no suite-wide timeout: a hanging test would hang CI"
    # Bounded on BOTH sides. Too small and normal tests flake (a worker's first
    # test pays for a testcontainers Postgres start); too large and the timeout
    # stops being a signal — the review's real hang ran 595 s.
    assert 30 <= timeout <= 300, f"timeout={timeout} is outside the useful band"


def test_the_timeout_is_live_in_THIS_session(request):
    """The setting could be present in the file and inert in the run — a missing
    plugin, or a `-p no:timeout`. Ask the session that is executing this test."""
    assert request.config.pluginmanager.hasplugin("timeout"), "the plugin is not loaded"
    # getini returns the raw string for this option, hence float() on both sides.
    assert float(request.config.getini("timeout")) == float(_pytest_options()["timeout"]), (
        "the effective timeout does not match pyproject.toml"
    )
