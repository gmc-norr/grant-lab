"""Slice C3 — gitio's subprocesses are bounded, and a timeout is a GitCommitError.

subprocess.TimeoutExpired is a sibling of CalledProcessError, not a subclass, so
adding `timeout=` without widening the handlers would skip write_and_commit_files'
whole restore-and-reset rollback and leak a raw exception out of the read helpers.
Every test here injects the timeout; none waits 30 seconds.
"""

from __future__ import annotations

import logging
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


def test_a_reset_OSError_does_not_mask_the_original_failure(tmp_path, monkeypatch):
    """The sibling above guards the reset against TimeoutExpired only, and that is
    NOT the whole of Task 6's binding constraint — the reset must not be able to
    raise out of the handler at all. A plain OSError is the live gap: ENOSPC or
    EIO mid-rollback, a permissions change, or an external actor removing the
    directory in the window between the failing commit and this reset all make
    the call's `cwd=` unusable, and the resulting FileNotFoundError would replace
    the GitCommitError every caller of write_and_commit_files is written to catch.

    (Not scrub_workspace_bytes: it removes only the attachments/ and references/
    subtrees, and ReferenceStorage.delete_workspace documents that sibling repo/
    is untouched.)
    """
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original\n")

    real_run = subprocess.run

    def _run(args, **kw):
        if args[:2] == ["git", "commit"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)
        if args[:2] == ["git", "reset"]:
            # What an unusable `cwd=` produces — a removed or renamed repo dir.
            raise FileNotFoundError(2, "No such file or directory", str(repo))
        return real_run(args, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

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


def test_a_forward_OSError_is_a_GitCommitError_and_the_rollback_still_runs(
    tmp_path, monkeypatch
):
    """The rollback covered TimeoutExpired and CalledProcessError but not OSError,
    and the forward path is full of it: the write_text loop, and every `cwd=` on
    add/diff/commit/rev-parse. ENOSPC or EIO mid-write, a permissions change, or
    an externally removed directory sent a raw OSError past the whole
    restore-and-reset block — leaving a half-written file on disk while the
    caller's DB transaction rolled back, and denying the caller the
    GitCommitError all eight production call sites are written against.

    Same shape as the timeout sibling at the top of this file: `git add` really
    runs, so the tree is genuinely dirty when the commit dies.
    """
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original\n")

    real_run = subprocess.run

    def _run(args, **kw):
        if args[:2] == ["git", "commit"]:
            raise FileNotFoundError(2, "No such file or directory", str(repo))
        return real_run(args, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

    with pytest.raises(GitCommitError) as exc:
        write_and_commit_files(repo, {PROPOSAL_FILENAME: "rewritten\n"}, "msg")
    # Its own branch — NOT the timeout wording, which would misreport a
    # filesystem error as a stall.
    assert "FileNotFoundError" in str(exc.value)
    assert "timed out" not in str(exc.value)

    # The rollback ran: file content restored and the index reset.
    assert (repo / PROPOSAL_FILENAME).read_text() == "original\n"
    staged = real_run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.strip() == "", f"index left dirty: {staged.stdout!r}"


def test_a_suppressed_reset_error_still_reaches_the_caller(tmp_path, monkeypatch):
    """A reset failure is suppressed so it can't mask the original error, but
    suppressed must not mean traceless: this is exactly the case that leaves a
    stale index behind, and it used to say nothing at all."""
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original\n")

    real_run = subprocess.run

    def _run(args, **kw):
        if args[:2] == ["git", "commit"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)
        if args[:2] == ["git", "reset"]:
            raise OSError(28, "No space left on device")
        return real_run(args, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

    with pytest.raises(GitCommitError) as exc:
        write_and_commit_files(repo, {PROPOSAL_FILENAME: "rewritten\n"}, "msg")

    # The original failure still leads; the rollback error rides along.
    assert "timed out" in str(exc.value)
    assert "rollback encountered" in str(exc.value)
    assert "No space left on device" in str(exc.value)


def test_log_if_timeout_warns_on_a_timeout_and_stays_silent_otherwise(caplog):
    """_log_if_timeout and its four call sites shipped with zero coverage, so a
    refactor that widened the predicate to OSError — reporting a plain
    filesystem error as a stall — or that dropped the calls entirely would go
    green. Assert both directions."""
    caplog.set_level(logging.WARNING, logger=gitio.logger.name)

    gitio._log_if_timeout(
        subprocess.TimeoutExpired(
            cmd=["git", "commit", "-q", "-m", "msg"],
            timeout=gitio._GIT_TIMEOUT_SECONDS,
        )
    )
    records = [r for r in caplog.records if r.name == gitio.logger.name]
    assert len(records) == 1, f"expected exactly one WARNING, got {records!r}"
    assert records[0].levelno == logging.WARNING
    assert records[0].event == "gitio_subprocess_timeout"
    assert records[0].timeout_seconds == gitio._GIT_TIMEOUT_SECONDS

    # A non-timeout OSError is NOT a timeout and must not be reported as one.
    caplog.clear()
    gitio._log_if_timeout(OSError(28, "No space left on device"))
    assert [r for r in caplog.records if r.name == gitio.logger.name] == []


def test_a_git_argv_never_carries_document_text_into_a_log_or_an_error(
    tmp_path, monkeypatch, caplog
):
    """`git commit -m <message>` renders the message, and those messages are built
    from live document content — editing/save.py interpolates a proposal section
    heading, tools/citations.py embeds a citekey. This is an internal hospital
    tool: a section heading in a log line is content leaving the place the user
    put it, and the diagnostic value is in WHICH command died, not its arguments.
    """
    secret = "Patient-Cohort-Recruitment-Heading"
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original\n")

    real_run = subprocess.run

    def _run(args, **kw):
        if args[:2] == ["git", "commit"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)
        return real_run(args, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", _run)
    caplog.set_level(logging.WARNING, logger=gitio.logger.name)

    with pytest.raises(GitCommitError) as exc:
        write_and_commit_files(
            repo, {PROPOSAL_FILENAME: "rewritten\n"}, f"user edit: {secret}"
        )

    # The error string names the command, not the argv tail.
    assert "['git', 'commit']" in str(exc.value)
    assert secret not in str(exc.value)

    # And neither does the WARNING.
    records = [r for r in caplog.records if r.name == gitio.logger.name]
    assert len(records) == 1
    assert records[0].cmd == "['git', 'commit']"
    assert secret not in str(records[0].__dict__)


def test_cmd_head_never_falls_through_to_a_full_command():
    """TimeoutExpired.cmd is whatever was handed to run(), so the shapes other
    than list must be guarded too rather than rendered whole."""
    assert gitio._cmd_head(["git", "commit", "-m", "secret"]) == "['git', 'commit']"
    assert gitio._cmd_head(("git", "commit", "-m", "secret")) == "['git', 'commit']"
    assert gitio._cmd_head("git commit -m secret") == "git commit"
    # Anything else degrades to a type name — never the value.
    assert gitio._cmd_head({"m": "secret"}) == "<dict>"
