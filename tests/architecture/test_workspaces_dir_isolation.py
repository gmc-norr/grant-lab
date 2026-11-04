"""No test may write into the operator's live workspaces directory.

Settings no longer has a workspaces_dir (the workspace left master on 2026-11-03),
but the directory and its repos are still on disk, and migrations 0037, 0066 and 0068
read WORKSPACES_DIR straight from os.environ, defaulting to "var/workspaces" — the
directory the live deployment holds. Migration tests that downgrade and re-upgrade run
those backfills inside a test. The autouse `_workspaces_dir_is_never_the_real_one`
fixture in tests/conftest.py redirects WORKSPACES_DIR; this pins it.
"""

from __future__ import annotations

import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
PRODUCTION_DEFAULT = (REPO / "var" / "workspaces").resolve()
_READERS = (
    "0037_backfill_reference_original_sha256.py",
    "0066_workspace_current_branch.py",
    "0068_branch_entity.py",
)


def test_the_env_never_resolves_to_the_real_workspaces_dir():
    value = os.environ.get("WORKSPACES_DIR")
    assert value, "the autouse fixture in tests/conftest.py did not set WORKSPACES_DIR"
    assert pathlib.Path(value).resolve() != PRODUCTION_DEFAULT


def test_the_migrations_still_default_to_the_thing_being_guarded_against():
    """Control: if a reader stops using WORKSPACES_DIR or changes its default, the
    test above guards nothing — fail loudly so this file is updated."""
    for name in _READERS:
        src = (REPO / "alembic" / "versions" / name).read_text()
        assert 'os.environ.get("WORKSPACES_DIR", "var/workspaces")' in src, name
