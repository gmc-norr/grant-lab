"""Atomic multi-file git commit for the citation tools.

Writes one or more files in a workspace's git repo and commits them in a single
commit, so proposal.md and citations.json always move together. On any git
failure every touched file is restored to its pre-write state (newly-created
files are removed) and the index is reset (scoped reset, never --hard), so a
failed mutation leaves the working tree exactly as it was.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "call-manager")
    env.setdefault("GIT_AUTHOR_EMAIL", "call-manager@local")
    env.setdefault("GIT_COMMITTER_NAME", "call-manager")
    env.setdefault("GIT_COMMITTER_EMAIL", "call-manager@local")
    return env


class GitCommitError(RuntimeError):
    """Raised when the atomic commit fails (after rollback)."""


def write_and_commit_files(
    repo_dir: Path,
    files: dict[str, str],
    message: str,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Write each {relative_path: content} file under repo_dir and commit them
    atomically. Returns the new commit SHA. On failure, restores all touched
    files to their pre-write state and re-raises GitCommitError.

    ``files`` maps repo-relative paths to their full new text content.
    If the staged diff is empty (no actual change), returns the current HEAD
    SHA without creating a new commit.
    """
    repo_dir = Path(repo_dir)
    env = env or _git_env()
    paths = list(files.keys())

    # Capture pre-write state for rollback: original text or None if absent.
    originals: dict[str, str | None] = {}
    for rel in paths:
        p = repo_dir / rel
        originals[rel] = p.read_text() if p.exists() else None

    try:
        # Write all files.
        for rel, content in files.items():
            (repo_dir / rel).write_text(content)

        # Stage all files.
        subprocess.run(
            ["git", "add", *paths],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            env=env,
        )

        # Nothing staged (identical content) → return current HEAD without committing.
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir,
            capture_output=True,
            env=env,
        )
        if diff.returncode == 0:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            return head.stdout.strip()

        subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            env=env,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return head.stdout.strip()

    except subprocess.CalledProcessError as e:
        # ROLLBACK: scoped to the touched paths only — NEVER --hard.
        for rel, orig in originals.items():
            p = repo_dir / rel
            if orig is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(orig)
        subprocess.run(
            ["git", "reset", "HEAD", "--", *paths],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            env=env,
        )
        stderr = (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        raise GitCommitError(f"atomic commit failed: {stderr.strip()}") from e
