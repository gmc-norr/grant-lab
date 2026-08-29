"""Git-history tools: get_artifact_history, read_artifact_at, diff_artifact.

Read-only — never mutate the repo. All shell out to git; errors are
returned as {"error": "..."} dicts so the model sees them rather than
the SDK crashing.

Rev3 / Codex M5: every git command is scoped to proposal.md only.
Without the scope, commits to sibling phased artifacts
(proposal-spec.md, proposal-plan.md, review files) leak into the
returned proposal.md history.

SECURITY (Codex round 3): every agent-supplied "sha" argument MUST be
validated by ports/shas.py (^[0-9a-fA-F]{7,64}$) BEFORE being passed to git.
Without the regex guard a value like `--output=proposal.md` would be
parsed by git as an option (it's recognized before the `--` path
separator), letting the model overwrite proposal.md via `git diff
--output=...` and bypass edit_proposal/replace_proposal, the
auto-commit, and the artifact_versions write. The regex blocks any
input that could plausibly be a git option.

CONCURRENCY: `subprocess.run` blocks, and these handlers run on the single
asyncio loop the whole instance shares — a `git log` on a large workspace
stalls every other request, socket and scheduler future for its duration
(ruff ASYNC221). So each git call lives in a SYNC helper below that the
async handler awaits in a threadpool.

`asyncio.to_thread` and not starlette's `run_in_threadpool`: this module is
under the fence's import gate, which allows stdlib only. Same semantics, no
framework dependency in a fenced layer.

Those helpers are plain functions on purpose. `to_thread(subprocess.run,
[...])` would pass the sink as a NAME rather than call it, and
tests/fencing/sink_guard.py's `_is_subprocess_call` only inspects `ast.Call`
nodes — so all three git sinks would vanish from the fence's view. Keeping
the call literal keeps the subcommand provable; the pins in
`PROVEN_NON_COMMIT` name these helpers.
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from call_manager.proposal_assist.ports.shas import sha_error as _validate_sha
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext


def _git_log_proposal(repo_dir, limit: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "log",
            f"-n{limit}",
            "--pretty=format:%H%x00%ct%x00%s",
            "--",
            PROPOSAL_FILENAME,
        ],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_show_proposal(repo_dir, commit_sha: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "show", f"{commit_sha}:{PROPOSAL_FILENAME}"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_diff_proposal(repo_dir, commit_a: str, commit_b: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "diff", commit_a, commit_b, "--", PROPOSAL_FILENAME],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


async def get_artifact_history_impl(
    ctx: ToolContext,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Recent commits that TOUCHED proposal.md, newest-first, with sha,
    message, and timestamp.

    Rev3 / Codex M5: scoped with `-- proposal.md` so commits to other
    phased artifacts (proposal-spec.md, proposal-plan.md, review files,
    Phase 0's initial-empty commit) don't leak into the returned list.
    """
    limit = max(1, min(limit, 100))  # clamp
    try:
        proc = await asyncio.to_thread(_git_log_proposal, ctx.repo_dir, limit)
    except subprocess.CalledProcessError as e:
        return {"error": f"git log failed: {e.stderr}"}
    commits = []
    for line in proc.stdout.strip().split("\n"):
        if not line:
            continue
        sha, ts, message = line.split("\x00", 2)
        commits.append({"sha": sha, "timestamp": int(ts), "message": message})
    return {"commits": commits}


async def read_artifact_at_impl(
    ctx: ToolContext,
    *,
    commit_sha: str,
) -> dict[str, Any]:
    """Return proposal.md content at a specific commit."""
    err = _validate_sha(commit_sha)
    if err:
        return {"error": err}
    try:
        proc = await asyncio.to_thread(_git_show_proposal, ctx.repo_dir, commit_sha)
    except subprocess.CalledProcessError as e:
        return {"error": f"git show failed for {commit_sha}: {e.stderr.strip()}"}
    return {"content": proc.stdout}


async def diff_artifact_impl(
    ctx: ToolContext,
    *,
    commit_a: str,
    commit_b: str,
) -> dict[str, Any]:
    """Unified diff between two commits on proposal.md."""
    err = _validate_sha(commit_a, arg_name="commit_a") or _validate_sha(
        commit_b, arg_name="commit_b"
    )
    if err:
        return {"error": err}
    try:
        proc = await asyncio.to_thread(_git_diff_proposal, ctx.repo_dir, commit_a, commit_b)
    except subprocess.CalledProcessError as e:
        return {"error": f"git diff failed: {e.stderr.strip()}"}
    return {"diff": proc.stdout}
