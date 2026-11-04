"""Migrations 0037, 0066 and 0068 carry frozen copies of the workspace code they
used to import (2026-11-03). A migration has to keep running after the code it
once called is gone, so those three import nothing from `grant_lab` at all, and
no migration may import the removed package.

G1 (`git grep proposal_assist -- src`) cannot see this: alembic/ is outside src/.
Imports inside upgrade() count, which is why this walks the whole tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
FROZEN = (
    "0037_backfill_reference_original_sha256.py",
    "0066_workspace_current_branch.py",
    "0068_branch_entity.py",
)


def _imported_modules(path: Path) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
    return out


@pytest.mark.parametrize("name", FROZEN)
def test_a_frozen_migration_imports_no_application_code(name):
    mods = [m for m in _imported_modules(VERSIONS / name) if m.split(".")[0] == "grant_lab"]
    assert mods == [], f"{name} imports {mods}"


def test_no_migration_imports_the_removed_workspace_package():
    offenders = sorted(
        f"{path.name}: {mod}"
        for path in VERSIONS.glob("*.py")
        for mod in _imported_modules(path)
        if mod.startswith("grant_lab.proposal_assist")
    )
    assert offenders == [], offenders
