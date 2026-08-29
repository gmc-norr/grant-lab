"""git branch primitives. Every expectation here was measured against real git
on 2026-08-29 before being written down — including the one that is
counter-intuitive (an unborn HEAD still has a branch NAME)."""

import subprocess

import pytest

from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    branch_exists,
    checkout_branch,
    create_branch,
    current_branch,
    delete_branch,
    has_commits,
    is_valid_branch_name,
    list_branches,
)


def _repo(tmp_path, *, commit=True):
    d = tmp_path / "repo"
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=d, check=True)
    if commit:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "first"],
            cwd=d,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "PATH": "/usr/bin:/bin",
            },
        )
    return d


def test_current_branch_reads_the_checked_out_branch(tmp_path):
    assert current_branch(_repo(tmp_path)) == "main"


def test_current_branch_works_on_an_UNBORN_head(tmp_path):
    # Measured: `git symbolic-ref --short HEAD` exits 0 and prints "main" on a
    # repo with no commits. The migration depends on this — a workspace still in
    # setup must not fail the backfill.
    assert current_branch(_repo(tmp_path, commit=False)) == "main"


def test_current_branch_raises_on_a_DETACHED_head(tmp_path):
    d = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    with pytest.raises(GitCommitError):
        current_branch(d)


def test_create_then_list_and_exists(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "aims-ambitious")
    assert sorted(list_branches(d)) == ["aims-ambitious", "main"]
    assert branch_exists(d, "aims-ambitious") is True
    assert branch_exists(d, "nope") is False
    # Creating must NOT move HEAD.
    assert current_branch(d) == "main"


def test_create_on_an_unborn_repo_raises(tmp_path):
    # Measured: `git branch x HEAD` exits 128 with
    # "fatal: not a valid object name: 'HEAD'".
    with pytest.raises(GitCommitError):
        create_branch(_repo(tmp_path, commit=False), "x")


def test_has_commits_distinguishes_unborn_from_born(tmp_path):
    # current_branch CANNOT make this distinction — it succeeds on both.
    assert has_commits(_repo(tmp_path)) is True
    assert has_commits(_repo(tmp_path / "b", commit=False)) is False


def test_checkout_moves_head(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "short-version")
    checkout_branch(d, "short-version")
    assert current_branch(d) == "short-version"


def test_delete_removes_the_ref(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "dead-end")
    delete_branch(d, "dead-end")
    assert branch_exists(d, "dead-end") is False


def test_delete_is_force_and_removes_an_UNMERGED_branch(tmp_path):
    # -D not -d: the branch a user most wants to throw away is the one whose
    # commits went nowhere, and `-d` refuses exactly that.
    d = _repo(tmp_path)
    create_branch(d, "abandoned")
    checkout_branch(d, "abandoned")
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "work"],
        cwd=d,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        },
    )
    checkout_branch(d, "main")
    delete_branch(d, "abandoned")
    assert branch_exists(d, "abandoned") is False


def test_delete_of_the_current_branch_raises(tmp_path):
    # git refuses this itself (exit 1, "cannot delete branch ... used by
    # worktree"). The app refuses first, but this proves the floor exists.
    d = _repo(tmp_path)
    with pytest.raises(GitCommitError):
        delete_branch(d, "main")


@pytest.mark.parametrize("name", ["a..b", "foo.lock", "-lead", "with space", ""])
def test_invalid_names_are_rejected(name):
    # Delegated to `git check-ref-format --branch`. Measured 2026-08-29: git
    # rejects each of these. Do NOT hand-roll the rules.
    assert is_valid_branch_name(name) is False


@pytest.mark.parametrize("name", ["good-name", "aims/ambitious", "v2"])
def test_valid_names_are_accepted(name):
    assert is_valid_branch_name(name) is True
