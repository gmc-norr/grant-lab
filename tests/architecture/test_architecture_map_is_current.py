"""The architecture map must match the code it is generated from.

A stale map is worse than no map: it is confidently wrong, which is exactly the
failure this map exists to prevent. So the map is pinned here, in pytest, and not
only in CI — the repo's other drift check (schema.gen.ts) lives in
.github/workflows/ci.yml alone, which means `uv run pytest` passes green against a
stale generated file. This one does not.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/gen_architecture.py"
MAP = REPO / "docs/ARCHITECTURE.md"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_architecture", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_map_matches_the_code():
    """Regenerate and diff. Failing here means the code moved and the map did not."""
    assert MAP.exists(), f"{MAP} is missing — run: uv run python {SCRIPT.name}"
    expected = _generator().render()
    actual = MAP.read_text()
    assert actual == expected, (
        "docs/ARCHITECTURE.md is stale. Regenerate it in the same commit that moved "
        "the code:\n    uv run python scripts/gen_architecture.py"
    )


def test_the_generator_never_imports_the_application():
    """It parses with ast so it runs with no database, no settings, no SDK.

    If this is ever relaxed, the map becomes unrunnable in exactly the situations
    where a reader most needs it.
    """
    src = SCRIPT.read_text()
    assert "import call_manager" not in src
    assert "from call_manager" not in src


@pytest.mark.parametrize(
    "table,predicate",
    [
        # The three facts an unmeasured inference got wrong. Pinned by hand so the
        # map's own summary line can never quietly stop being true.
        ("coherence_jobs", "status IN ('queued','running')"),
        ("turn_jobs", "status = 'running'"),
        ("claim_analysis_jobs", "status = 'queued'"),
    ],
)
def test_the_active_uniqueness_section_carries_the_real_predicate(table, predicate):
    body = MAP.read_text()
    row = [ln for ln in body.splitlines() if f"`{table}`" in ln and "uq_" in ln]
    assert row, f"no active-uniqueness row for {table} in the map"
    assert any(f"`{predicate}`" in ln for ln in row), (
        f"{table}'s predicate in the map is not {predicate!r}: {row}"
    )
