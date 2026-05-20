"""Git-history tools: get_artifact_history, read_artifact_at, diff_artifact.

Read-only — never mutate the repo. All shell out to git; errors are
returned as {"error": "..."} dicts so the model sees them rather than
the SDK crashing.

Rev3 / Codex M5: every git command is scoped to proposal.md only.
Without the scope, commits to sibling phased artifacts
(proposal-spec.md, proposal-plan.md, review files) leak into the
returned proposal.md history.

SECURITY (Codex round 3): every agent-supplied "sha" argument MUST be
validated to match ^[0-9a-fA-F]{7,40}$ BEFORE being passed to git.
Without the regex guard a value like `--output=proposal.md` would be
parsed by git as an option (it's recognized before the `--` path
separator), letting the model overwrite proposal.md via `git diff
--output=...` and bypass edit_proposal/replace_proposal, the
auto-commit, and the artifact_versions write. The regex blocks any
input that could plausibly be a git option.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext

# Git allows shas as short as 4 chars (configurable via core.abbrev),
# but our tools only produce 7+ (matches git's --abbrev-commit default).
# The upper bound 40 is sha1 full length. sha256-mode repos use 64; if
# this codebase ever migrates, extend.
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _validate_sha(sha: str, *, arg_name: str = "commit_sha") -> str | None:
    """Return an error message if `sha` is unsafe to pass to git, else None.

    Anything not matching the hex-sha pattern is rejected. This blocks
    git-option injection (e.g. --output=...) — see the module docstring.
    """
    if not isinstance(sha, str) or not _SHA_PATTERN.match(sha):
        return f"{arg_name} must be a hex commit sha (7-40 chars, 0-9a-f); got: {sha!r}"
    return None


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
        proc = subprocess.run(
            [
                "git",
                "log",
                f"-n{limit}",
                "--pretty=format:%H%x00%ct%x00%s",
                "--",
                PROPOSAL_FILENAME,
            ],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
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
        proc = subprocess.run(
            ["git", "show", f"{commit_sha}:{PROPOSAL_FILENAME}"],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
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
        proc = subprocess.run(
            ["git", "diff", commit_a, commit_b, "--", PROPOSAL_FILENAME],
            cwd=ctx.repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return {"error": f"git diff failed: {e.stderr.strip()}"}
    return {"diff": proc.stdout}
