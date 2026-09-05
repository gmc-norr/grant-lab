"""No test may write into the operator's live workspaces directory.

The suite shares a checkout with a running deployment, and Settings.workspaces_dir
defaults to "var/workspaces" — the very directory that deployment uses. Every full
run created 16 real workspace repos there; 488 of the 497 directories present when
this was found were test litter. The damage was not corruption (tests use their own
database) but evidence: a survey of branch usage across "287 workspace repos" was
counting test fixtures, and a feature decision was taken on it.

The isolation lives in the autouse `_workspaces_dir_is_never_the_real_one` fixture
in tests/conftest.py. This pins it, because a silently-removed fixture reintroduces
the pollution with every test still green.
"""

from __future__ import annotations

import pathlib

import pytest

from grant_lab.config import Settings

REPO = pathlib.Path(__file__).resolve().parents[2]
PRODUCTION_DEFAULT = (REPO / "var" / "workspaces").resolve()


@pytest.fixture
def _required_env(monkeypatch):
    """The four settings Settings() cannot be built without. Deliberately does
    NOT touch WORKSPACES_DIR — that is the thing under test."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def test_settings_never_resolve_to_the_real_workspaces_dir(_required_env):
    """The resolved directory a test would write to is not the deployment's.

    Resolved, not compared as a string: "var/workspaces" is relative, and pytest
    runs from the repo root, so the string and the real path are the same place.
    """
    resolved = pathlib.Path(Settings().workspaces_dir).resolve()
    assert resolved != PRODUCTION_DEFAULT, (
        f"a test would write workspace repos into {PRODUCTION_DEFAULT} — the live "
        "data directory. The autouse fixture in tests/conftest.py that redirects "
        "WORKSPACES_DIR is missing or has stopped taking effect."
    )


def test_the_production_default_is_still_the_thing_being_guarded_against():
    """Control. If the default is ever changed, the test above stops meaning
    anything — it would compare against a path nothing uses. This fails loudly
    instead, so the guard is updated rather than silently hollowed."""
    field = Settings.model_fields["workspaces_dir"]
    assert field.default == "var/workspaces", (
        f"Settings.workspaces_dir now defaults to {field.default!r}. Update "
        "PRODUCTION_DEFAULT in this file to match, or the isolation guard is inert."
    )
