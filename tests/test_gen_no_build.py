"""Unit tests for ``scripts/gen_no_build.py`` — the wheels-only allowlist
generator that gates every dependency change in CI (``--check``).

The script lives under ``scripts/`` (not a package, not on ``pythonpath``), so
we load it by file path. The tests drive the pure logic against synthetic
``uv.lock`` / ``pyproject.toml`` files in ``tmp_path`` — no real I/O, no network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gen_no_build.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_no_build", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_no_build = _load_module()


# A minimal uv.lock-shaped TOML: two third-party deps with wheels, plus our own
# editable project (which must be skipped). Names deliberately out of order so
# the test also pins the sorting contract.
LOCK_IN_SYNC = """\
[[package]]
name = "zlib-pkg"
version = "1.0"
wheels = [{ url = "https://example/zlib_pkg-1.0-py3-none-any.whl" }]

[[package]]
name = "alpha-pkg"
version = "2.0"
wheels = [{ url = "https://example/alpha_pkg-2.0-py3-none-any.whl" }]

[[package]]
name = "call-manager"
version = "0.1.0"
source = { editable = "." }
"""

# pyproject text with a no-build-package array matching LOCK_IN_SYNC (sorted).
PYPROJECT_IN_SYNC = """\
[tool.uv]
exclude-newer = "2026-06-02T00:00:00Z"
no-build-package = [
    "alpha-pkg",
    "zlib-pkg",
]
"""


def _write(tmp_path: Path, lock: str, pyproject: str) -> tuple[Path, Path]:
    lock_path = tmp_path / "uv.lock"
    pyproject_path = tmp_path / "pyproject.toml"
    lock_path.write_text(lock)
    pyproject_path.write_text(pyproject)
    return lock_path, pyproject_path


def test_check_passes_when_in_sync(tmp_path, capsys):
    lock_path, pyproject_path = _write(tmp_path, LOCK_IN_SYNC, PYPROJECT_IN_SYNC)

    rc = gen_no_build.main(
        ["--check"], lock_path=lock_path, pyproject_path=pyproject_path
    )

    assert rc == 0
    assert "in sync" in capsys.readouterr().out
    # --check must never mutate pyproject.toml.
    assert pyproject_path.read_text() == PYPROJECT_IN_SYNC


def test_check_fails_when_entry_missing(tmp_path, capsys):
    # Drop "alpha-pkg" from the allowlist -> drift.
    drifted = PYPROJECT_IN_SYNC.replace('    "alpha-pkg",\n', "")
    lock_path, pyproject_path = _write(tmp_path, LOCK_IN_SYNC, drifted)

    rc = gen_no_build.main(
        ["--check"], lock_path=lock_path, pyproject_path=pyproject_path
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "out of sync" in err
    assert "scripts/gen_no_build.py" in err  # actionable: tells you how to fix


def test_check_fails_when_extra_entry(tmp_path, capsys):
    # An entry not present in the lock -> drift.
    drifted = PYPROJECT_IN_SYNC.replace(
        '    "zlib-pkg",\n', '    "zlib-pkg",\n    "stale-pkg",\n'
    )
    lock_path, pyproject_path = _write(tmp_path, LOCK_IN_SYNC, drifted)

    rc = gen_no_build.main(
        ["--check"], lock_path=lock_path, pyproject_path=pyproject_path
    )

    assert rc == 1
    assert "out of sync" in capsys.readouterr().err


def test_source_only_package_fails_structurally(tmp_path, capsys):
    # A dep with no "wheels" key is source-only and cannot be allowlisted.
    lock = LOCK_IN_SYNC + (
        '\n[[package]]\nname = "sdist-only-pkg"\nversion = "3.0"\n'
        'sdist = { url = "https://example/sdist_only_pkg-3.0.tar.gz" }\n'
    )
    lock_path, pyproject_path = _write(tmp_path, lock, PYPROJECT_IN_SYNC)

    rc = gen_no_build.main(
        ["--check"], lock_path=lock_path, pyproject_path=pyproject_path
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "source-only" in err
    assert "sdist-only-pkg" in err


def test_editable_and_virtual_packages_are_skipped():
    lock = {
        "package": [
            {"name": "real-dep", "wheels": [{"url": "x.whl"}]},
            {"name": "call-manager", "source": {"editable": "."}, "wheels": []},
            {"name": "workspace-root", "source": {"virtual": "."}},
        ]
    }

    with_wheels, source_only = gen_no_build.classify_packages(lock)

    assert with_wheels == ["real-dep"]
    assert source_only == []
    # First-party packages never leak into either third-party list.
    assert "call-manager" not in with_wheels
    assert "workspace-root" not in with_wheels


def test_generate_mode_rewrites_pyproject(tmp_path, capsys):
    # No --check: out-of-sync pyproject gets rewritten to match the lock.
    drifted = PYPROJECT_IN_SYNC.replace('    "alpha-pkg",\n', "")
    lock_path, pyproject_path = _write(tmp_path, LOCK_IN_SYNC, drifted)

    rc = gen_no_build.main([], lock_path=lock_path, pyproject_path=pyproject_path)

    assert rc == 0
    assert "Updated no-build-package" in capsys.readouterr().out
    # After regeneration, --check must pass.
    rc_check = gen_no_build.main(
        ["--check"], lock_path=lock_path, pyproject_path=pyproject_path
    )
    assert rc_check == 0


def test_classify_sorts_names():
    lock = {
        "package": [
            {"name": "zebra", "wheels": [{"url": "z.whl"}]},
            {"name": "apple", "wheels": [{"url": "a.whl"}]},
        ]
    }
    with_wheels, _ = gen_no_build.classify_packages(lock)
    assert with_wheels == ["apple", "zebra"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
