"""Slice C3 — gitio's subprocesses are bounded, and a timeout is a GitCommitError.

subprocess.TimeoutExpired is a sibling of CalledProcessError, not a subclass, so
adding `timeout=` without widening the handlers would skip write_and_commit_files'
whole restore-and-reset rollback and leak a raw exception out of the read helpers.
Every test here injects the timeout; none waits 30 seconds.
"""

from __future__ import annotations

import subprocess

import pytest

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.citations import gitio
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    current_head,
    write_and_commit_files,
)
from tests.proposal_assist.conftest import init_workspace_repo


def test_a_timed_out_commit_is_a_GitCommitError_and_leaves_the_tree_clean(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original\n")

    real_run = subprocess.run

    def _run(args, **kw):
        # Let `git add` succeed so the tree really is dirty when the commit dies —
        # this is the exact window where skipping the rollback loses data.
        if args[:2] == ["git", "commit"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)
        return real_run(args, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

    with pytest.raises(GitCommitError) as exc:
        write_and_commit_files(repo, {PROPOSAL_FILENAME: "rewritten\n"}, "msg")
    assert "timed out" in str(exc.value)

    # The rollback ran: file content restored and the index reset.
    assert (repo / PROPOSAL_FILENAME).read_text() == "original\n"
    staged = real_run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.strip() == "", f"index left dirty: {staged.stdout!r}"


def test_a_reset_timeout_does_not_mask_the_original_failure(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original\n")

    real_run = subprocess.run

    def _run(args, **kw):
        if args[:2] == ["git", "commit"] or args[:2] == ["git", "reset"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)
        return real_run(args, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

    # GitCommitError, NOT a bare TimeoutExpired from inside the handler.
    with pytest.raises(GitCommitError):
        write_and_commit_files(repo, {PROPOSAL_FILENAME: "rewritten\n"}, "msg")


def test_a_read_helper_timeout_is_a_GitCommitError(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original\n")

    def _run(args, **kw):
        raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

    with pytest.raises(GitCommitError):
        current_head(repo)
