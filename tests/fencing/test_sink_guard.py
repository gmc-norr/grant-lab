"""Fence F3e1a — a third way to run git must stop the build.

Spec 2026-08-21 §3.2. The declared sink list is only trustworthy if nothing can
commit outside it, so each clause is proven against a fixture that violates it AND
run over the real tree.
"""

from __future__ import annotations

from tests.fencing.sink_guard import (
    check_execution_surfaces,
    check_no_git_library_imports,
    check_no_git_library_in_manifests,
    check_no_shell_true,
    check_no_stray_git_commit,
)


def test_the_real_tree_has_no_stray_git_commit():
    assert check_no_stray_git_commit() == []


def test_the_real_tree_uses_only_subprocess():
    assert check_execution_surfaces() == []


def test_the_real_tree_has_no_shell_true():
    assert check_no_shell_true() == []


def test_a_stray_subprocess_git_commit_is_caught():
    from tests.fencing.sink_guard import _stray_git_commit_in

    src = (
        "import subprocess\n"
        "def f(d):\n"
        "    subprocess.run(['git', 'commit', '-m', 'x'], cwd=d)\n"
    )
    assert _stray_git_commit_in(src, "x/rogue.py")


def test_os_system_is_caught():
    from tests.fencing.sink_guard import _execution_surfaces_in

    assert _execution_surfaces_in("import os\ndef f():\n    os.system('git commit')\n", "x/r.py")


def test_shell_true_is_caught():
    from tests.fencing.sink_guard import _shell_true_in

    src = "import subprocess\ndef f():\n    subprocess.run('git commit -m x', shell=True)\n"
    assert _shell_true_in(src, "x/r.py")


def test_no_pin_is_stale():
    """Every entry in PROVEN_NON_COMMIT must still correspond to a call the guard would
    report if the allowlist were empty. A pin whose function moved, or whose call was
    removed, is stale — it silently exempts nothing and must LEAVE the list. This is the
    direction that makes the allowlist shrink (mirrors the import gate's
    no-stale-exemption test). Step 3 mandates it; it is the SEVENTH test, not the sixth."""
    from tests.fencing.sink_guard import PROVEN_NON_COMMIT, pristine_stray_git_report

    reported = pristine_stray_git_report()  # every fail-closed hit, allowlist ignored
    stale = [pin for pin in PROVEN_NON_COMMIT if pin not in reported]
    assert not stale, f"pins the guard no longer reports (remove them): {stale}"


def test_the_real_tree_imports_no_git_library():
    """Spec §13 criterion 2 (import half): no src/ file imports git/pygit2/dulwich."""
    assert check_no_git_library_imports() == []


def test_no_git_library_in_manifests():
    """Spec §13 criterion 2 (manifest half): pyproject.toml/uv.lock declare no git lib."""
    assert check_no_git_library_in_manifests() == []


def test_a_git_library_import_is_caught():
    """_git_library_import_in catches import git / from pygit2 / import dulwich
    without false-positiving on unrelated names that contain 'git' as a substring."""
    from tests.fencing.sink_guard import _git_library_import_in

    # Positive: plain import
    assert _git_library_import_in("import git\n", "x/r.py")
    # Positive: dotted import
    assert _git_library_import_in("import git.repo\n", "x/r.py")
    # Positive: from-import form
    assert _git_library_import_in("from pygit2 import Repository\n", "x/r.py")
    # Positive: dulwich
    assert _git_library_import_in("import dulwich\n", "x/r.py")
    # Negative: unrelated name that starts with 'git' (gitlab)
    assert not _git_library_import_in("import gitlab_unrelated\n", "x/r.py")
    # Negative: stdlib — 'git' substring in unrelated module name
    assert not _git_library_import_in("import subprocess\n", "x/r.py")


def test_a_git_library_in_a_manifest_is_caught():
    """_manifest_declares_git_lib catches gitpython/pygit2/dulwich (case-insensitive)
    without false-positiving on manifests that don't contain those patterns."""
    from tests.fencing.sink_guard import _manifest_declares_git_lib

    # Positive: GitPython (case-insensitive)
    assert _manifest_declares_git_lib('dependencies = ["GitPython==3.1"]')
    # Positive: pygit2
    assert _manifest_declares_git_lib("requires = ['pygit2']")
    # Positive: dulwich
    assert _manifest_declares_git_lib("dulwich>=0.20")
    # Negative: benign manifest with no git library
    assert not _manifest_declares_git_lib('dependencies = ["requests", "fastapi"]')
