"""Atomic multi-file git commit for the citation tools.

Writes one or more files in a workspace's git repo and commits them in a single
commit, so proposal.md and citations.json always move together. On any git
failure every touched file is restored to its pre-write state (newly-created
files are removed) and the index is reset (scoped reset, never --hard), so a
failed mutation leaves the working tree exactly as it was.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Ceiling on any SINGLE git subprocess (not on the function: write_and_commit_files
# runs up to four of these serially plus one in its rollback, and checkpoint_artifact
# calls current_head before it — so gitio can hold the commit mutex for ~180s in the
# worst case).
#
# 30s is deliberately far above the 2s lock cap because a git timeout means the
# filesystem is broken, not that two writers collided. But be clear about what that
# costs, because it is NOT consistent with the budgets the rest of Slice C3 derives
# from: a git stall holds the mutex past the DELETE route's 10s acquisition cap, so
# the owner's DELETE answers 429 having deleted nothing, and blows the 6.0s budget
# that governs MAX_CONTENDED_ROWS. That is an accepted trade — unbounded became
# finite, which is the whole point — but it is a trade, not an absence of one.
_GIT_TIMEOUT_SECONDS = 30.0


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "call-manager")
    env.setdefault("GIT_AUTHOR_EMAIL", "call-manager@local")
    env.setdefault("GIT_COMMITTER_NAME", "call-manager")
    env.setdefault("GIT_COMMITTER_EMAIL", "call-manager@local")
    return env


def _cmd_head(cmd: object) -> str:
    """Render only the first two argv elements ('git commit'), never the rest.

    The tail of a git argv carries live document text: write_and_commit_files'
    commit message interpolates a proposal section heading (editing/save.py) and
    a citekey (tools/citations.py). This is an internal hospital tool with a
    PII-conscious posture, and a section heading in a log line is content leaving
    the place the user put it. The diagnostic value is entirely in WHICH command
    died, not in its arguments.

    ``TimeoutExpired.cmd`` is whatever was handed to ``run()``, so accept any
    shape: a str command (shell=True) is split to its first two tokens rather
    than rendered whole, and anything else degrades to its type name — no branch
    may fall through to the full value.
    """
    if isinstance(cmd, (list, tuple)):
        return str([str(part) for part in cmd[:2]])
    if isinstance(cmd, str):
        return " ".join(cmd.split()[:2])
    return f"<{type(cmd).__name__}>"


def _log_if_timeout(exc: BaseException) -> None:
    """Make a git timeout LOUD, which is what _GIT_TIMEOUT_SECONDS' comment
    promises and what nothing here delivered.

    A timeout funnels into GitCommitError, and several callers turn that into
    silence: collab/checkpoint.py's _head_or_empty maps it to "" — which reads as
    "this workspace has no repo yet" — and downstream that becomes a quiet
    committed=False, or a 404 "no proposal-spec.md yet" on the spec-lock route,
    for what is actually a filesystem stall. Deliberately NOT a GitTimeoutError
    subclass: that would change what every one of those callers catches.

    One helper rather than an inline branch at each site because the read helpers
    catch (OSError, TimeoutExpired) together — a non-timeout OSError must not be
    reported as a timeout, and that discrimination should exist once.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        logger.warning(
            "gitio.subprocess_timeout",
            extra={
                "event": "gitio_subprocess_timeout",
                "cmd": _cmd_head(exc.cmd),
                "timeout_seconds": _GIT_TIMEOUT_SECONDS,
            },
        )


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

    Once the write loop begins, every failure mode of the forward path — a
    non-zero git exit, a subprocess timeout, or an ``OSError`` from the writes or
    from ``cwd=`` — runs the rollback and surfaces as ``GitCommitError``.

    Before that point two things can still raise something else, and both do so
    with nothing yet modified, so there is nothing to roll back: the argument
    guards raise ``TypeError``/``ValueError``, and the pre-write ``originals``
    capture reads each existing file, so it can raise a raw ``OSError``. Callers
    written against ``GitCommitError`` alone should read that as "the repo is
    untouched", not as "cannot happen".

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

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        # OSError belongs here for the same reason the two subprocess errors do:
        # every forward call above — the write_text loop, add, diff, commit,
        # rev-parse — can raise one (ENOSPC or EIO mid-write, a permissions
        # change, an unreadable cwd), and an OSError escaping RAW skips this
        # whole rollback and leaves a half-written file on disk while the
        # caller's DB transaction rolls back. It also replaces the
        # GitCommitError all eight production callers are written against.
        # current_head, blob_sha and read_blob already translate OSError; this
        # was the one function that did not, and the only one with a rollback.
        #
        # FIX 1: ROLLBACK — scoped to the touched paths only, NEVER --hard.
        # Each per-file restore is guarded so a disk/perms error can't prevent
        # the index reset from running and can't swallow the original error.
        restore_exc: BaseException | None = None
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

        # A failure HERE must not mask the original one: the per-file restore
        # above has already run, and the caller needs the real error. check=False
        # covers a non-zero exit; the timeout and OSError need catching. The
        # OSError triggers are the same ones that bring us into this handler at
        # all — ENOSPC or EIO mid-write, a permissions change, or an external
        # actor removing the directory in the window between a failing forward
        # call and this reset — and a bare FileNotFoundError out of the handler
        # would replace the GitCommitError every caller expects.
        #
        # (An earlier revision of this comment blamed scrub_workspace_bytes for
        # removing repo_dir. It does not: workspaces.py drops only the
        # attachments/ and references/ subtrees, and the docstring on
        # ReferenceStorage.delete_workspace states that the sibling repo/ tree
        # is untouched. The guard is still required — see the widened handler
        # above — but not for that reason.)
        #
        # Suppressed, NOT silent: a swallowed reset error leaves a stale index
        # and says nothing, which is precisely the scenario this guard exists
        # for. Fold it into the same rollback diagnostic when nothing has
        # claimed that slot, so it reaches the caller either way.
        try:
            subprocess.run(
                ["git", "reset", "HEAD", "--", *paths],
                cwd=repo_dir,
                check=False,
                capture_output=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as reset_exc:
            if restore_exc is None:
                restore_exc = reset_exc
        raw = getattr(e, "stderr", None) or b""
        stderr = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        _log_if_timeout(e)
        if isinstance(e, subprocess.TimeoutExpired):
            msg = (
                f"atomic commit timed out after {_GIT_TIMEOUT_SECONDS}s "
                f"running {_cmd_head(e.cmd)}: {stderr.strip()}"
            )
        elif isinstance(e, OSError):
            # Distinct wording from the timeout branch: this is a filesystem
            # error on a forward call, not a stall. OSError carries no .stderr,
            # so the exception itself is the whole diagnostic.
            msg = f"atomic commit failed with {type(e).__name__}: {e}"
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
        _log_if_timeout(exc)
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
        _log_if_timeout(exc)
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
        _log_if_timeout(exc)
        raise GitCommitError(f"cat-file {sha} failed: {exc}") from exc
    if proc.returncode != 0:
        raise GitCommitError(
            f"cat-file {sha} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout.decode(errors="replace")
