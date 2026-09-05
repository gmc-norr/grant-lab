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
import re
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

    spa = tmp_path / "src" / "grant_lab" / "web" / "spa"
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
    spa = root / "src" / "grant_lab" / "web" / "spa"
    before = sorted(p.name for p in spa.iterdir())

    assert _run(root, VITE_APP_EDITION="match").returncode == 1

    assert spa.is_dir(), "the served directory was deleted on the way to failing"
    assert sorted(p.name for p in spa.iterdir()) == before
    assert (spa / SENTINEL).read_text() == "the previously served bundle"


def test_a_clean_bundle_packages_and_replaces_the_previous_one(tmp_path):
    root = _tree(tmp_path, modules=CLEAN)
    out = _run(root, VITE_APP_EDITION="match")
    assert out.returncode == 0, out.stdout + out.stderr
    spa = root / "src" / "grant_lab" / "web" / "spa"
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
    assert (root / "src/grant_lab/web/spa/edition.txt").read_text().startswith("edition=match")


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


# --- the served tree has exactly one writer ----------------------------------
# package_spa.sh is the gate: it asks dist/module-manifest.json which edition the
# bundle actually is and stamps src/grant_lab/web/spa/edition.txt with the
# answer. That only governs the deployed artefact if nothing ELSE writes that
# directory. Two things did — ci.yml's `docker cp` for the wheel and
# collab_e2e_run.sh's `rsync --delete` — and both were invisible, because
# web/spa_serving.py treats an ABSENT marker as unknown-and-allowed. So the wheel
# shipped a full SPA past every gate, and a local prod e2e run erased the marker a
# `pnpm package:match` had left. Both now call the script. This keeps it true.

_SERVED = "src/grant_lab/web/spa"

# Write verbs, shell and Python. `docker cp` matches on `cp`. Deliberately NOT a
# bare `>`: YAML block scalars are `run: >`.
_WRITE = re.compile(
    r"\b(rm|rsync|cp|mkdir|mv|tee|install|unzip|tar"
    r"|rmtree|copytree|copy2|unlink|write_text|write_bytes|rename|replace|makedirs)\b"
)
# `SPA="$ROOT/src/grant_lab/web/spa"` — an assignment that captures the path.
_ASSIGN = re.compile(r"^(?:export\s+|let\s+|const\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$")


def _scanned_files() -> list[Path]:
    """Every file that could plausibly write the served tree.

    The first version scanned only `scripts/*.sh`, workflow YAML and one
    Dockerfile. Three outside reviewers independently pointed out that a Python
    script or a Makefile would simply not be looked at.
    """
    files = [
        *sorted((REPO / "scripts").glob("*.sh")),
        *sorted((REPO / "scripts").glob("*.py")),
        *sorted((REPO / "scripts").glob("*.mjs")),
        *sorted((REPO / ".github" / "workflows").glob("*.yml")),
        REPO / "frontend" / "Dockerfile",
        REPO / "Dockerfile",
        REPO / "Makefile",
        REPO / "frontend" / "package.json",
        REPO / "package.json",
    ]
    return [f for f in files if f.is_file()]


def _writer_lines(text: str, name: str) -> list[str]:
    """Lines in `text` that WRITE the served tree, directly or through a variable.

    Variable indirection is the whole point of this pass. The first version
    required the literal path and a write verb on the SAME line — and
    `package_spa.sh`, the one script that certainly writes the tree, does
    `SPA="$ROOT/src/grant_lab/web/spa"` and then `rm -rf "$SPA"`. Measured: the
    detector flagged ZERO lines in it. It only ever passed because that file is
    skipped by name, and a brand-new rogue script copying the same idiom sailed
    through all fifteen tests. Found independently by three outside models;
    reproduced here before fixing.

    The alias set is computed to a fixpoint, so `X="$SPA"` counts too.
    """
    lines = text.splitlines()
    aliases: set[str] = set()
    for _ in range(4):  # fixpoint; 4 is far more indirection than anyone writes
        before = len(aliases)
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _ASSIGN.match(stripped)
            if not m:
                continue
            name_, rhs = m.group(1), m.group(2)
            if _SERVED in rhs or any(f"${a}" in rhs or f"${{{a}}}" in rhs for a in aliases):
                aliases.add(name_)
        if len(aliases) == before:
            break

    hits = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _ASSIGN.match(stripped) and not _WRITE.search(stripped):
            continue  # a bare assignment is not a write
        names_path = _SERVED in stripped or any(
            f"${a}" in stripped or f"${{{a}}}" in stripped or f"{a})" in stripped for a in aliases
        )
        if names_path and _WRITE.search(stripped):
            hits.append(f"{name}:{i}  {stripped}")
    return hits


def test_the_scan_set_is_not_empty():
    """A scan over nothing passes over anything. Pin the files that matter."""
    names = {f.name for f in _scanned_files()}
    assert {"package_spa.sh", "collab_e2e_run.sh", "ci.yml", "Dockerfile"} <= names, names


def test_the_detector_sees_the_real_writer():
    """THE control that cannot rot.

    `test_no_other_writer_of_the_served_tree` below skips `package_spa.sh` by
    filename. That skip is only honest if the detector would otherwise flag it —
    otherwise the whole scan is an absence that was already true. So: point the
    detector at the one file that certainly writes the tree and require it to fire.
    It writes through `$SPA`, so this fails on any detector that cannot follow a
    variable.
    """
    flagged = _writer_lines((REPO / "scripts" / "package_spa.sh").read_text(), "package_spa.sh")
    assert len(flagged) >= 3, (
        "the detector cannot see the writes in the one script that definitely "
        f"writes the served tree — it is not detecting anything: {flagged}"
    )
    joined = " ".join(flagged)
    assert "rm -rf" in joined and "cp -R" in joined, flagged


def test_the_writer_detector_actually_detects():
    """The historical forms. These three lines were really in the tree."""
    flagged = _writer_lines(
        "rsync -a --delete frontend/dist/ src/grant_lab/web/spa/\n"
        "rm -rf src/grant_lab/web/spa && mkdir -p src/grant_lab/web/spa\n"
        'docker cp "$c:/build/dist/." src/grant_lab/web/spa/\n',
        "fixture",
    )
    assert len(flagged) == 3, flagged


def test_the_detector_follows_variable_indirection():
    """The form that defeated the first version, in shell and in Python."""
    shell = _writer_lines(
        'SPA_DIR="src/grant_lab/web/spa"\n'
        'rm -rf "$SPA_DIR"\n'
        'mkdir -p "$SPA_DIR"\n'
        'cp -R frontend/dist/. "$SPA_DIR"/\n',
        "rogue.sh",
    )
    assert len(shell) == 3, shell

    chained = _writer_lines(
        'SPA="$ROOT/src/grant_lab/web/spa"\nDEST="$SPA"\nrsync -a x/ "$DEST"/\n',
        "rogue2.sh",
    )
    assert len(chained) == 1, chained

    python_writer = _writer_lines(
        'SPA = REPO / "src/grant_lab/web/spa"\nshutil.rmtree(SPA)\nshutil.copytree(DIST, SPA)\n',
        "rogue.py",
    )
    assert len(python_writer) == 2, python_writer


def test_the_detector_does_not_flag_reads_or_comments():
    """collab_e2e_run.sh legitimately READS the path (a git check-ignore safety
    net) and several files discuss it in comments. Flagging those would push
    someone to delete the safety net to get green. A bare assignment is not a
    write either — only a write verb applied to the alias is."""
    assert (
        _writer_lines(
            '  if ! git check-ignore -q "src/grant_lab/web/spa/"; then\n'
            "# NOTE: rm -rf src/grant_lab/web/spa used to live here\n"
            '  SPA="$ROOT/src/grant_lab/web/spa"\n'
            '  echo "serving from $SPA"\n',
            "fixture",
        )
        == []
    )


def test_no_other_writer_of_the_served_tree():
    offenders: list[str] = []
    for f in _scanned_files():
        if f.name == "package_spa.sh":
            continue  # the gate itself; pinned by test_the_detector_sees_the_real_writer
        offenders += _writer_lines(f.read_text(), str(f.relative_to(REPO)))
    assert offenders == [], (
        "these write src/grant_lab/web/spa without going through "
        "scripts/package_spa.sh, so the bundle gate and edition.txt do not "
        "govern what they leave behind:\n" + "\n".join(offenders)
    )


def test_the_wheel_build_packages_through_the_script():
    """The other half. 'Nothing else writes it' is also satisfied by a CI that
    stopped writing it at all — which would ship a wheel with no SPA."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "bash scripts/package_spa.sh" in ci


def test_the_container_build_does_not_hard_code_the_edition():
    """frontend/Dockerfile built the FULL edition unconditionally, so no
    `--build-arg` could ever produce a match wheel."""
    dockerfile = (REPO / "frontend" / "Dockerfile").read_text()
    assert "ARG VITE_APP_EDITION=full" in dockerfile
    build_lines = [ln for ln in dockerfile.splitlines() if "pnpm run build" in ln]
    assert len(build_lines) == 1, build_lines
    assert "$VITE_APP_EDITION" in build_lines[0], build_lines[0]
