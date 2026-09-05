"""The git-history tools must not run `git` on the event loop.

This instance runs ONE asyncio loop under a single-instance constraint, so a
`subprocess.run(["git", "log", ...])` inside an `async def` stalls every other
request, socket and scheduler future for as long as git takes — and `git log`
on a large workspace is not fast.

Ruff's ASYNC221 is now the floor (pyproject `select`), but a lint rule proves a
call SHAPE, not that the work left the loop: rewriting the helper to be `async`
would silence the rule and restore the bug. So this asserts the thing that
matters — the subprocess runs on a different thread than the loop.
"""

import asyncio
import subprocess
import threading
from types import SimpleNamespace

import pytest

from grant_lab.proposal_assist.tools import history


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "proposal.md").write_text("# P\n\nbody\n")
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "-C", str(tmp_path), "add", "proposal.md"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True, env=env
    )
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return SimpleNamespace(repo_dir=str(tmp_path)), sha


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["history", "read", "diff"])
async def test_git_runs_off_the_event_loop_thread(tool, tmp_path, monkeypatch):
    ctx, sha = _repo(tmp_path)
    loop_thread = threading.get_ident()
    seen: list[int] = []

    # Record the thread each git call actually runs on, then delegate.
    for name in ("_git_log_proposal", "_git_show_proposal", "_git_diff_proposal"):
        real = getattr(history, name)

        def spy(*a, _real=real, **kw):
            seen.append(threading.get_ident())
            return _real(*a, **kw)

        monkeypatch.setattr(history, name, spy)

    if tool == "history":
        out = await history.get_artifact_history_impl(ctx, limit=5)
        assert out["commits"], out
    elif tool == "read":
        out = await history.read_artifact_at_impl(ctx, commit_sha=sha)
        assert "body" in out["content"], out
    else:
        out = await history.diff_artifact_impl(ctx, commit_a=sha, commit_b=sha)
        assert out["diff"] == "", out

    assert seen, "the git helper was never called — the spy did not take"
    assert loop_thread not in seen, (
        f"{tool}: git ran on the event loop thread — every other request, socket "
        "and scheduler future was blocked for its duration"
    )


@pytest.mark.anyio
async def test_the_loop_keeps_running_while_git_does(tmp_path, monkeypatch):
    # The consequence, stated directly: other loop work must make progress
    # while a slow git call is in flight.
    ctx, _ = _repo(tmp_path)
    real = history._git_log_proposal

    def slow(*a, **kw):
        import time

        time.sleep(0.25)
        return real(*a, **kw)

    monkeypatch.setattr(history, "_git_log_proposal", slow)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await history.get_artifact_history_impl(ctx, limit=5)
    beat.cancel()
    # A blocking call would have let through 0-1 ticks in 250 ms.
    assert ticks >= 10, f"loop advanced only {ticks} ticks during a 250 ms git call"
