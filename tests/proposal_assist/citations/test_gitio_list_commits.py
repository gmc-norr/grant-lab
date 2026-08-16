"""list_commits: bounded, newest-first, truncation detectable.

Inspection (retention Slice B) shows an admin the history of a deleted
workspace. History grows with every save, so the read must be bounded at the
source rather than trimmed after the fact.
"""

import subprocess
from pathlib import Path

import pytest

from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    list_commits,
)


def _repo(tmp_path: Path, n_commits: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    for i in range(n_commits):
        (repo / "proposal.md").write_text(f"# draft {i}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", f"commit {i}"],
            cwd=repo,
            check=True,
        )
    return repo


def test_returns_commits_newest_first(tmp_path):
    repo = _repo(tmp_path, 3)
    commits, truncated = list_commits(repo, limit=10)
    assert [c.subject for c in commits] == ["commit 2", "commit 1", "commit 0"]
    assert truncated is False


def test_truncates_at_the_limit_and_says_so(tmp_path):
    repo = _repo(tmp_path, 5)
    commits, truncated = list_commits(repo, limit=2)
    assert [c.subject for c in commits] == ["commit 4", "commit 3"]
    assert truncated is True, (
        "a caller that cannot tell a capped list from a complete one will "
        "present a partial history as the whole history"
    )


def test_exactly_at_the_limit_is_not_truncated(tmp_path):
    """The off-by-one that matters: 2 commits with limit=2 is complete, not truncated."""
    repo = _repo(tmp_path, 2)
    commits, truncated = list_commits(repo, limit=2)
    assert len(commits) == 2
    assert truncated is False


def test_missing_repository_raises(tmp_path):
    with pytest.raises(GitCommitError):
        list_commits(tmp_path / "nope", limit=10)


def test_repository_with_no_commits_raises(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    with pytest.raises(GitCommitError):
        list_commits(repo, limit=10)


HOSTILE = "fix: a|b|c pipes \x1f US \x1e RS \t tab and \\n literal"


def test_a_hostile_subject_round_trips_exactly(tmp_path):
    """Commit subjects are user-controlled and git stores control bytes verbatim
    — verified: `git commit -m $'a\\x1eb'` succeeds and `%s` emits the 0x1e back.
    So ASCII RS/US framing is forgeable: a subject containing the record
    separator splits one commit into two rows, and one containing the field
    separator shifts every field.

    NUL is the one byte git refuses ("error: a NUL byte in commit log message
    not allowed"), which is why the implementation frames on it.

    Exact equality, and no `or` — an assertion with an alternative branch passes
    for whichever branch happens to hold and can never fail."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    (repo / "proposal.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", HOSTILE],
        cwd=repo,
        check=True,
    )
    commits, truncated = list_commits(repo, limit=10)
    assert len(commits) == 1, "a forged separator split one commit into several rows"
    assert commits[0].subject == HOSTILE
    assert truncated is False


def test_a_hostile_subject_does_not_corrupt_its_neighbours(tmp_path):
    """The failure mode framing exists to prevent: injection in commit 2 of 3
    must not shift commit 1's or commit 3's fields."""
    repo = _repo(tmp_path, 1)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", HOSTILE],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "after"],
        cwd=repo,
        check=True,
    )
    commits, _ = list_commits(repo, limit=10)
    assert [c.subject for c in commits] == ["after", HOSTILE, "commit 0"]
    assert all(len(c.sha) == 40 for c in commits), "a shifted field lands in sha"


def test_a_plain_directory_does_not_inherit_an_enclosing_repository(tmp_path):
    """`repo_dir` may exist without being a repository — a workspace deleted
    before setup ever ran `git init`. Without GIT_CEILING_DIRECTORIES git walks
    up and reports the ENCLOSING repository's history, so an admin would be
    shown this project's commits as if they were the workspace's.

    Mirrors the ceiling that `current_head` already sets (gitio.py:290)."""
    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=outer, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "enclosing repo commit"],
        cwd=outer,
        check=True,
    )
    with pytest.raises(GitCommitError):
        list_commits(outer / "inner", limit=10)
