"""scripts/package_spa.sh must validate BEFORE it deletes, and read the edition
from the bundle rather than from the caller's environment variable.

This is the only automated gate on the packaging step. Without it the script's
two load-bearing properties are protected by one manual run that will never run
again — and one of them ("move the rm -rf below the gate check") is called out in
the plan as the thing that goes wrong.

The script derives ROOT from BASH_SOURCE, so a fixture tree is enough: copy the
two scripts in, fake a dist, and run it. No build, no node_modules, ~1 s.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SENTINEL = "do-not-delete-me.txt"


def _tree(tmp_path: Path, *, modules: list[str]) -> Path:
    """A minimal repo-shaped fixture with a dist whose manifest we control."""
    (tmp_path / "scripts").mkdir()
    for name in ("package_spa.sh", "check_match_bundle.mjs"):
        shutil.copy(REPO / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "scripts" / "package_spa.sh").chmod(0o755)

    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>")
    (dist / "module-manifest.json").write_text(json.dumps(modules))

    spa = tmp_path / "src" / "call_manager" / "web" / "spa"
    spa.mkdir(parents=True)
    (spa / SENTINEL).write_text("the previously served bundle")
    return tmp_path


def _run(root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "package_spa.sh")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(root), **env},
        check=False,
    )


CLEAN = ["/x/frontend/src/routes/Dashboard.tsx", "/x/frontend/src/shell/SimpleShell.tsx"]
DIRTY = [*CLEAN, "/x/frontend/src/cockpit/CockpitSidebar.tsx"]


def test_a_full_bundle_asked_for_as_match_is_refused(tmp_path):
    root = _tree(tmp_path, modules=DIRTY)
    out = _run(root, VITE_APP_EDITION="match")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "refusing to package a match build" in out.stderr
    assert "CockpitSidebar" in out.stderr, "the refusal must name the offending modules"


def test_the_refusal_does_not_destroy_the_served_tree(tmp_path):
    """The mutation this file exists for: move `rm -rf` above the gate and this
    is what goes red. A script that deletes before it validates has already
    destroyed the served bundle by the time it reports the failure."""
    root = _tree(tmp_path, modules=DIRTY)
    spa = root / "src" / "call_manager" / "web" / "spa"
    before = sorted(p.name for p in spa.iterdir())

    assert _run(root, VITE_APP_EDITION="match").returncode == 1

    assert spa.is_dir(), "the served directory was deleted on the way to failing"
    assert sorted(p.name for p in spa.iterdir()) == before
    assert (spa / SENTINEL).read_text() == "the previously served bundle"


def test_a_clean_bundle_packages_and_replaces_the_previous_one(tmp_path):
    root = _tree(tmp_path, modules=CLEAN)
    out = _run(root, VITE_APP_EDITION="match")
    assert out.returncode == 0, out.stdout + out.stderr
    spa = root / "src" / "call_manager" / "web" / "spa"
    assert (spa / "index.html").exists()
    assert not (spa / SENTINEL).exists(), "the previous bundle must not survive"
    assert not (spa / "module-manifest.json").exists(), "a build artefact, not an asset"


@pytest.mark.parametrize("env", [{}, {"VITE_APP_EDITION": "full"}])
def test_the_edition_comes_from_the_bundle_not_the_variable(tmp_path, env):
    """A .env-configured match build sets no shell variable (vite.config.ts reads
    loadEnv, not process.env), and `./scripts/package_spa.sh` after a full build
    sets none either. Keying the label off the variable got both wrong."""
    root = _tree(tmp_path, modules=CLEAN)
    out = _run(root, **env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "edition=match" in out.stdout, out.stdout
    assert (root / "src/call_manager/web/spa/edition.txt").read_text().startswith("edition=match")


def test_a_full_bundle_packages_as_full(tmp_path):
    root = _tree(tmp_path, modules=DIRTY)
    out = _run(root)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "edition=full" in out.stdout


def test_it_refuses_a_tree_that_is_not_the_repo_root(tmp_path):
    """ROOT comes from BASH_SOURCE, so a symlinked scripts/ dir resolves it to the
    wrong tree — and the next statement is an rm -rf."""
    root = _tree(tmp_path, modules=CLEAN)
    (root / "scripts" / "check_match_bundle.mjs").unlink()
    out = _run(root, VITE_APP_EDITION="match")
    assert out.returncode == 1
    assert "is not the repo root" in out.stderr


# --- The bundle gate's MECHANISM, not just its data -------------------------
#
# frontend/src/checkMatchBundle.test.ts pins the EXCLUDED array's membership.
# Nothing pinned the code that USES it: neutering the `EXCLUDED.some(...)`
# predicate, or the non-zero exit, left that test, `pnpm check:bundle` and CI all
# green while the gate waved a cockpit module through. That is the same shape as
# the five hollow gates this branch already found, so it gets a positive control:
# a manifest that MUST be rejected, asserted to be rejected.


def _gate(root, dist):
    return subprocess.run(
        ["node", str(root / "scripts" / "check_match_bundle.mjs"), str(dist)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_bundle_gate_rejects_a_manifest_containing_an_excluded_module(tmp_path):
    root = _tree(tmp_path, modules=DIRTY)
    out = _gate(root, root / "frontend" / "dist")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "CockpitSidebar" in out.stderr, "the refusal must name the offending module"


def test_the_bundle_gate_accepts_a_clean_manifest(tmp_path):
    """The other half. Without this a gate that rejected EVERYTHING would pass
    the test above while making every match build unshippable."""
    root = _tree(tmp_path, modules=CLEAN)
    out = _gate(root, root / "frontend" / "dist")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "clean" in out.stdout
