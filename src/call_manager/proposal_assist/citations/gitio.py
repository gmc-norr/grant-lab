"""Atomic multi-file git commit for the citation tools.

Writes one or more files in a workspace's git repo and commits them in a single
commit, so proposal.md and citations.json always move together. On any git
failure every touched file is restored to its pre-write state (newly-created
files are removed) and the index is reset (scoped reset, never --hard), so a
failed mutation leaves the working tree exactly as it was.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

# Ceiling on any single git subprocess. write_and_commit_files runs inside a
# mutex-held transaction (via checkpoint_artifact), so an untimed git call stalls
# the mutex for every other writer exactly as an unbounded lock wait did before
# Slice C3 — and no lock timeout can bound a subprocess.
#
# Far above the 2s lock cap on purpose: a git timeout is a genuine filesystem
# failure, not routine contention, so it should be rare and loud rather than a
# routine refusal. Matches phases/setup.py:108's _GIT_TIMEOUT_SECONDS.
_GIT_TIMEOUT_SECONDS = 30.0


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
    # Option-injection guard: reject any path key beginning with '-' before the
    # traversal check so they can never reach 'git add'.
    repo_root = Path(repo_dir).resolve()
    for rel in paths:
        if rel.startswith("-"):
            raise ValueError(f"path key must not begin with '-': {rel!r}")
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
            ["git", "add", "--", *paths],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )

        # Nothing staged (identical content) → return current HEAD without committing.
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
        if diff.returncode == 0:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=env,
            )
            return head.stdout.strip()

        subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
        return head.stdout.strip()

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
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

        # A timeout HERE must not mask the original failure: the per-file restore
        # above has already run, and the caller needs the real error. check=False
        # covers a non-zero exit; only the timeout needs catching.
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(
                ["git", "reset", "HEAD", "--", *paths],
                cwd=repo_dir,
                check=False,
                capture_output=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=env,
            )
        raw = getattr(e, "stderr", None) or b""
        stderr = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if isinstance(e, subprocess.TimeoutExpired):
            msg = f"atomic commit timed out after {_GIT_TIMEOUT_SECONDS}s: {stderr.strip()}"
        else:
            msg = f"atomic commit failed: {stderr.strip()}"
        if restore_exc is not None:
            msg += f"; additionally, rollback encountered a restore error: {restore_exc}"
        raise GitCommitError(msg) from e


def current_head(repo_dir: Path) -> str:
    """Return the current HEAD commit SHA of *repo_dir*. Raises GitCommitError
    on failure (no repo / no commits).

    GIT_CEILING_DIRECTORIES pins repo discovery: if *repo_dir* exists but is
    not itself a git repo, git must NOT walk up and silently report an
    enclosing repo's HEAD (e.g. the project repo in dev layouts). Ceiling
    dirs don't apply when cwd IS the repo root — the legitimate case — so
    real workspace repos are unaffected.
    """
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo_dir).resolve().parent))
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(repo_dir),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # OSError: FileNotFoundError, NotADirectoryError, PermissionError.
        raise GitCommitError(f"rev-parse HEAD failed: {exc}") from exc
    if proc.returncode != 0:
        raise GitCommitError(f"rev-parse HEAD failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def blob_sha(repo_dir: Path, ref: str, path: str) -> str:
    """Return the git blob SHA of *path* at *ref* (e.g. 'HEAD'). Used to pin the
    exact bytes of a committed review file so the read endpoint can fetch them by
    SHA. GIT_CEILING_DIRECTORIES pins discovery exactly like current_head()."""
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo_dir).resolve().parent))
    try:
        proc = subprocess.run(
            ["git", "rev-parse", f"{ref}:{path}"],
            cwd=Path(repo_dir),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitCommitError(f"rev-parse {ref}:{path} failed: {exc}") from exc
    if proc.returncode != 0:
        raise GitCommitError(f"rev-parse {ref}:{path} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def read_blob(repo_dir: Path, sha: str) -> str:
    """Return the content of a git blob by SHA (`git cat-file -p`), decoded with
    errors="replace" so a corrupt or binary blob can never raise UnicodeDecodeError
    up into the endpoint (review files are text, but be defensive — Codex Low).
    GIT_CEILING_DIRECTORIES pinned like current_head(). Caller validates the SHA
    shape before passing it."""
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo_dir).resolve().parent))
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-p", sha],
            cwd=Path(repo_dir),
            capture_output=True,  # bytes (NO text=True) so WE control decoding
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitCommitError(f"cat-file {sha} failed: {exc}") from exc
    if proc.returncode != 0:
        raise GitCommitError(
            f"cat-file {sha} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout.decode(errors="replace")
