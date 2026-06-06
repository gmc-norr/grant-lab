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

    Note: if ``git rev-parse HEAD`` fails after a successful commit,
    ``GitCommitError`` is raised but the commit still exists on disk. Callers
    should treat this identically to the post-commit DB-failure path in
    proposal.py — reconciliation handles the drift (same atomicity model).

    Delete semantics (``None`` values) are explicitly out of scope; pass only
    ``str`` content.
    """
    repo_dir = Path(repo_dir)
    env = env or _git_env()
    paths = list(files.keys())

    # FIX 2: Reject None (or any non-str) values loudly.
    # Delete-semantics are out of scope for this function; callers must pass
    # str content only.
    for rel, v in files.items():
        if not isinstance(v, str):
            raise TypeError(f"file content must be str, got {type(v)} for {rel!r}")

    # FIX 3: Path-traversal guard — every path must stay inside repo_dir.
    repo_root = Path(repo_dir).resolve()
    for rel in paths:
        resolved = (repo_root / rel).resolve()
        if not resolved.is_relative_to(repo_root):
            raise ValueError(f"path escapes repo_dir: {rel!r}")

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
        # FIX 1: ROLLBACK — scoped to the touched paths only, NEVER --hard.
        # Each per-file restore is guarded so a disk/perms error can't prevent
        # the index reset from running and can't swallow the original error.
        restore_exc: OSError | None = None
        for rel, orig in originals.items():
            p = repo_dir / rel
            try:
                if orig is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_text(orig)
            except OSError as oe:
                if restore_exc is None:
                    restore_exc = oe
                # Continue restoring the remaining files regardless.

        # Always run the scoped reset, even if a restore raised.
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
        msg = f"atomic commit failed: {stderr.strip()}"
        if restore_exc is not None:
            msg += f"; additionally, rollback encountered a restore error: {restore_exc}"
        raise GitCommitError(msg) from e
