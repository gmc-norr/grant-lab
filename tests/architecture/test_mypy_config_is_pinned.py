"""The `[tool.mypy]` table in pyproject.toml is pinned whole.

CI runs mypy, but mypy reads its strictness from this table. Adding
`ignore_errors = true` for `grant_lab.*`, a new key, or a new override would
switch checks off while every other test stays green. So the table must equal
EXPECTED exactly, overrides included. Changing the mypy config on purpose means
changing EXPECTED here in the same commit.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

EXPECTED = {
    "python_version": "3.12",
    "files": ["src/grant_lab"],
    "plugins": ["pydantic.mypy"],
    "warn_unused_ignores": True,
    "warn_redundant_casts": True,
    "overrides": [
        {"module": ["pgvector.*", "apscheduler.*"], "ignore_missing_imports": True},
    ],
}


def mypy_table(pyproject: Path) -> dict:
    return tomllib.loads(pyproject.read_text())["tool"]["mypy"]


def test_the_mypy_config_is_exactly_the_pinned_table():
    assert mypy_table(PYPROJECT) == EXPECTED


def test_the_pin_refuses_an_override_that_silences_our_own_code(tmp_path):
    assert mypy_table(PYPROJECT) == EXPECTED, "control: the real file must pass first"
    text = PYPROJECT.read_text()
    mutated = text.replace(
        "[tool.coverage.run]",
        '[[tool.mypy.overrides]]\nmodule = ["grant_lab.*"]\nignore_errors = true\n\n'
        "[tool.coverage.run]",
        1,
    )
    assert mutated != text, "no change made"
    copy_path = tmp_path / "pyproject.toml"
    copy_path.write_text(mutated)
    assert mypy_table(copy_path) != EXPECTED


def test_the_pin_refuses_an_added_key(tmp_path):
    text = PYPROJECT.read_text()
    mutated = text.replace(
        "warn_redundant_casts = true\n", "warn_redundant_casts = true\nfollow_imports = 'skip'\n", 1
    )
    assert mutated != text, "no change made"
    copy_path = tmp_path / "pyproject.toml"
    copy_path.write_text(mutated)
    assert mypy_table(copy_path) != EXPECTED
