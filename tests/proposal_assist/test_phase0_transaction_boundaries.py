"""Phase-0 milestone boundaries: locks, Call pinning, bounded retry."""

import subprocess

import pytest

from call_manager.proposal_assist.phases import setup as setup_mod


def test_git_runs_hermetically_and_with_a_timeout(tmp_path, monkeypatch):
    """Site git config, hooks and signing must not reach into M1's locked region,
    and every command must be able to time out rather than hang under the lock."""
    seen: dict = {}

    def _fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)
    setup_mod._run_git(["init", "-q", "-b", "main"], cwd=tmp_path)

    assert seen["kwargs"]["timeout"] == setup_mod._GIT_TIMEOUT_SECONDS
    assert "core.hooksPath=/dev/null" in seen["args"]
    assert "commit.gpgsign=false" in seen["args"]
    env = seen["kwargs"]["env"]
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_a_hung_git_raises_rather_than_blocking(tmp_path, monkeypatch):
    def _boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(setup_mod.subprocess, "run", _boom)
    with pytest.raises(subprocess.TimeoutExpired):
        setup_mod._run_git(["init"], cwd=tmp_path)
