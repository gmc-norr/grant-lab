"""TDD tests for the atomic multi-file git commit helper in citations.gitio.

Written first so all tests fail before gitio.py exists; they must all pass
after implementation. No database, no network — only real git in tmp_path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    write_and_commit_files,
)

# ---------------------------------------------------------------------------
# Shared git-repo fixture (mirrors _init_repo in test_tools_proposal.py)
# ---------------------------------------------------------------------------


def _init_repo(repo_dir: Path, initial_content: str = "# Proposal\n\nInitial.\n") -> None:
    """Create a git repo with an initial proposal.md commit."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text(initial_content)
    subprocess.run(["git", "add", "proposal.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    return repo_dir


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _head_sha(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _working_tree_clean(repo_dir: Path) -> bool:
    """True when git status --porcelain produces no output."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == ""


def _index_clean(repo_dir: Path) -> bool:
    """True when nothing is staged (git diff --cached --quiet returns 0)."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_dir,
        capture_output=True,
    )
    return result.returncode == 0


def _commit_count_since_initial(repo_dir: Path) -> int:
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


# ---------------------------------------------------------------------------
# Test 1: two-file atomic commit
# ---------------------------------------------------------------------------


def test_two_file_atomic_commit(repo: Path) -> None:
    """write_and_commit_files commits proposal.md + citations.json atomically."""
    initial_head = _head_sha(repo)

    sha = write_and_commit_files(
        repo,
        {
            "proposal.md": "# Proposal\n\nRevised.\n",
            "citations.json": '{"version": 1, "evidence": []}\n',
        },
        message="agent: add citations",
    )

    # Returned sha matches HEAD.
    assert sha == _head_sha(repo)
    assert sha != initial_head

    # Exactly one new commit (initial + this one = 2 total).
    assert _commit_count_since_initial(repo) == 2

    # Both files are present and working tree is clean.
    assert (repo / "proposal.md").read_text() == "# Proposal\n\nRevised.\n"
    assert (repo / "citations.json").read_text() == '{"version": 1, "evidence": []}\n'
    assert _working_tree_clean(repo)


# ---------------------------------------------------------------------------
# Test 2: rollback on commit failure
# ---------------------------------------------------------------------------


def test_rollback_on_commit_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On commit failure: proposal.md restored, citations.json removed,
    index clean, unrelated dirty file untouched, GitCommitError raised."""
    import subprocess as _sp

    # Pre-existing content.
    original_proposal = (repo / "proposal.md").read_text()

    # An unrelated tracked-but-dirty file to prove scoped reset.
    sidecar = repo / "sidecar.txt"
    sidecar.write_text("clean")
    _sp.run(["git", "add", "sidecar.txt"], cwd=repo, check=True)
    _sp.run(["git", "commit", "-q", "-m", "add sidecar"], cwd=repo, check=True)
    sidecar.write_text("DIRTY uncommitted edits")

    initial_head = _head_sha(repo)

    # Monkeypatch subprocess.run inside the gitio module so only
    # the commit call raises; add/reset still run for real.
    import call_manager.proposal_assist.citations.gitio as _gitio_mod

    real_run = _gitio_mod.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "commit":
            raise _sp.CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr=b"simulated commit failure",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_gitio_mod.subprocess, "run", fake_run)

    with pytest.raises(GitCommitError):
        write_and_commit_files(
            repo,
            {
                "proposal.md": "# Proposal\n\nShould not land.\n",
                "citations.json": '{"version": 1, "evidence": []}\n',
            },
            message="agent: this commit will fail",
        )

    # proposal.md is restored to original.
    assert (repo / "proposal.md").read_text() == original_proposal

    # citations.json was newly created — must be removed.
    assert not (repo / "citations.json").exists()

    # Index is clean (nothing staged).
    assert _index_clean(repo)

    # HEAD has not advanced.
    assert _head_sha(repo) == initial_head

    # Unrelated dirty file is untouched.
    assert sidecar.read_text() == "DIRTY uncommitted edits"


# ---------------------------------------------------------------------------
# Test 3: no-op when content is identical (empty staged diff)
# ---------------------------------------------------------------------------


def test_noop_returns_head_without_new_commit(repo: Path) -> None:
    """Calling with the same content already committed returns HEAD,
    no new commit is created."""
    initial_head = _head_sha(repo)
    count_before = _commit_count_since_initial(repo)

    # Exact same content that was committed initially.
    sha = write_and_commit_files(
        repo,
        {"proposal.md": "# Proposal\n\nInitial.\n"},
        message="agent: no-op attempt",
    )

    assert sha == initial_head
    assert _commit_count_since_initial(repo) == count_before
