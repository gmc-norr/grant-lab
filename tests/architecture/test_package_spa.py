"""scripts/package_spa.sh must validate BEFORE it deletes, and must stay the only
writer of the served tree. The script derives ROOT from BASH_SOURCE, so a fixture
tree is enough: copy the script in, fake a dist, and run it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SENTINEL = "do-not-delete-me.txt"


def _tree(tmp_path: Path) -> Path:
    # src/grant_lab/web/spa below is also what package_spa.sh's repo-root identity
    # check looks for (`[ -d "$ROOT/src/grant_lab/web" ]`).
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "package_spa.sh", tmp_path / "scripts" / "package_spa.sh")
    (tmp_path / "scripts" / "package_spa.sh").chmod(0o755)
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>")
    spa = tmp_path / "src" / "grant_lab" / "web" / "spa"
    spa.mkdir(parents=True)
    (spa / SENTINEL).write_text("the previously served bundle")
    return tmp_path


def _served(root: Path) -> Path:
    return root / "src" / "grant_lab" / "web" / "spa"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "package_spa.sh")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(root)},
        check=False,
    )


def test_a_missing_build_is_refused_and_the_served_tree_survives(tmp_path):
    """Move `rm -rf` above the checks and this goes red."""
    root = _tree(tmp_path)
    (root / "frontend" / "dist" / "index.html").unlink()
    spa = _served(root)
    before = sorted(p.name for p in spa.iterdir())
    out = _run(root)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "no build at" in out.stderr
    assert spa.is_dir(), "the served directory was deleted on the way to failing"
    assert sorted(p.name for p in spa.iterdir()) == before
    assert (spa / SENTINEL).read_text() == "the previously served bundle"


def test_a_built_bundle_packages_and_replaces_the_previous_one(tmp_path):
    root = _tree(tmp_path)
    out = _run(root)
    assert out.returncode == 0, out.stdout + out.stderr
    spa = _served(root)
    assert (spa / "index.html").exists()
    assert not (spa / SENTINEL).exists(), "the previous bundle must not survive"
    assert not (spa / "edition.txt").exists(), "one build: no edition marker"


def test_it_refuses_a_tree_that_is_not_the_repo_root(tmp_path):
    root = _tree(tmp_path)
    shutil.rmtree(root / "src" / "grant_lab" / "web")
    out = _run(root)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "is not the repo root" in out.stderr
    assert not _served(root).exists(), "refused, but wrote the served tree first"


# --- the served tree has exactly one writer ----------------------------------
# Every served tree must come through the script's checks, and that only holds if
# nothing ELSE writes the directory. ci.yml (the wheel) and scripts/e2e_run.sh
# (prod-mode e2e) once copied into it on their own; both now call the script.
# This keeps it true.

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
    assert {"package_spa.sh", "e2e_run.sh", "ci.yml", "Dockerfile"} <= names, names


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
    """e2e_run.sh legitimately READS the path (a git check-ignore safety
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
            continue  # the writer itself; pinned by test_the_detector_sees_the_real_writer
        offenders += _writer_lines(f.read_text(), str(f.relative_to(REPO)))
    assert offenders == [], (
        "these write src/grant_lab/web/spa without going through "
        "scripts/package_spa.sh, so its build checks do not govern what they "
        "leave behind:\n" + "\n".join(offenders)
    )


def test_the_wheel_build_packages_through_the_script():
    """The other half. 'Nothing else writes it' is also satisfied by a CI that
    stopped writing it at all — which would ship a wheel with no SPA."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "bash scripts/package_spa.sh" in ci
