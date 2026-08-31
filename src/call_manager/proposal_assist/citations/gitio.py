"""Atomic multi-file git commit for the citation tools.

Writes one or more files in a workspace's git repo and commits them in a single
commit, so proposal.md and citations.json always move together. On any git
failure every touched file is restored to its pre-write state (newly-created
files are removed) and the index is reset (scoped reset, never --hard), so a
failed mutation leaves the working tree exactly as it was.
"""

from __future__ import annotations

import io
import logging
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
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


def _git_env(repo_dir: Path | None = None) -> dict[str, str]:
    """The environment every git subprocess runs in.

    *repo_dir* fences git's repository DISCOVERY to that directory. Without it git
    walks UPWARD until it finds any repository, so a workspace repo whose `.git`
    is corrupt answers with whatever repo encloses it — and `var/workspaces/` sits
    inside the application's own checkout, so the answer is the SERVER'S branches,
    with exit 0 and no error. The reconciler would then found rows named after the
    server's branches and delete the workspace's real ones. Measured 2026-08-31:

        workspace repo, corrupt .git     -> prints the enclosing branch, rc=0
        workspace repo, no .git at all   -> prints the enclosing branch, rc=0
        either one, WITH the ceiling     -> "fatal: not a git repository", rc=128
        a HEALTHY repo, WITH the ceiling -> unchanged, rc=0

    The hazard was already known here: `_list_commits_env` (gitio.py:475) and
    `current_head` (gitio.py:305) each set this ceiling for themselves. `_git` —
    which every other primitive in this module goes through — did not. Fixing the
    shared primitive is what closes the remaining doors.

    The ceiling is the repo's PARENT: git searches `repo_dir` itself and stops
    before the parent, so a real repo still finds its own `.git` at cwd.

    Absolute, via `resolve()`: GIT_CEILING_DIRECTORIES silently IGNORES a relative
    entry, and `workspace_repo_dir` builds on WORKSPACES_DIR, which defaults to the
    relative "var/workspaces". A relative ceiling would look like a fix and be
    inert.
    """
    env = os.environ.copy()
    # Hermetic, matching `phases/setup.py:_git_env`. `_m1` calls
    # `backfill_branch_entities`, whose git reads come through HERE — so without
    # this, an operator's global git config reaches inside M1's locked region and
    # setup.py's docstring claim ("nothing an operator configures can introduce a
    # hook, a signing helper or an interactive prompt into M1's locked region")
    # is false. These reads are on workspace repos the app created itself, so
    # site and global config have nothing legitimate to contribute.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if repo_dir is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(Path(repo_dir).resolve().parent)
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


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in *repo_dir*, capturing text output. Never raises on
    a non-zero exit — callers decide what a failure means, because for some of
    these (`branch_exists`) a non-zero exit IS the answer."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=_git_env(repo_dir),
    )


def current_branch(repo_dir: Path) -> str:
    """The checked-out branch name.

    Succeeds on an UNBORN HEAD (a repo with no commits): git still knows which
    branch the first commit will land on, and that name is the right answer.
    Raises GitCommitError on a DETACHED HEAD, where there is no branch name and
    no safe default.
    """
    proc = _git(repo_dir, "symbolic-ref", "--short", "HEAD")
    if proc.returncode != 0:
        raise GitCommitError(f"HEAD is not on a branch in {repo_dir}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def head_branch(repo_dir: Path) -> str | None:
    """The checked-out branch name, or None when HEAD is DETACHED.

    `current_branch` cannot answer this: it raises GitCommitError for a detached
    HEAD and for a broken repository alike, so a caller that catches it treats a
    git failure as "detached" and acts on it. In the reconciler that meant a
    transient failure CLEARED a healthy root pointer.

    `-q` is what makes the two separable, and the exit codes are measured
    (2026-08-31, git on this box):

        attached            -> 0, prints the name
        DETACHED, with -q   -> 1, prints nothing
        DETACHED, without -q-> 128 ("fatal: ref HEAD is not a symbolic ref")
        not a repository    -> 128
        unborn HEAD         -> 0, prints the name the first commit will land on

    So only exit 1 means detached. Same discipline as `branch_exists`, and for
    the same reason: collapsing 128 into the benign answer reports a broken repo
    as a normal state.
    """
    proc = _git(repo_dir, "symbolic-ref", "--short", "-q", "HEAD")
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise GitCommitError(f"cannot read HEAD in {repo_dir}: {proc.stderr.strip()}")
    return proc.stdout.strip()


#: The prefix `%(refname)` puts on every local branch.
_HEADS_PREFIX = "refs/heads/"


def list_branches(repo_dir: Path) -> list[str]:
    """Every local branch, in git's own order. Empty only for a repo with no refs.

    `%(refname)` and strip, NOT `%(refname:short)`. The `:short` form shortens to
    the shortest UNAMBIGUOUS name, so it depends on what other refs exist.
    Measured 2026-08-31 with a branch `feature` and a TAG also called `feature`:

        git branch --format='%(refname:short)'   ->  heads/feature
        git show-ref --verify refs/heads/heads/feature  ->  rc=1

    So the branch reported its own name as one `branch_exists` answers NO for.
    `_real_refs` filters on exactly that, so the branch vanished from the
    reconciler's view, its row was deleted as an orphan, and if it was the root
    the pointer was cleared. `%(refname)` is always the full path:

        git branch --format='%(refname)'  ->  refs/heads/feature
    """
    proc = _git(repo_dir, "branch", "--format=%(refname)")
    if proc.returncode != 0:
        raise GitCommitError(f"cannot list branches in {repo_dir}: {proc.stderr.strip()}")
    if "broken ref" in proc.stderr:
        # A corrupt ref file makes `git branch` exit ZERO, warn on stderr, and
        # silently OMIT that branch. Measured 2026-08-31:
        #
        #     $ echo garbage > .git/refs/heads/feature
        #     $ git branch --format='%(refname)'
        #     warning: ignoring broken ref refs/heads/feature
        #     refs/heads/main
        #     rc=0
        #
        # An incomplete listing that reports success is worse than a failure:
        # `backfill_branch_entities` treats every name it does not see as an
        # ORPHAN and DELETES its row — and clears the root pointer if it named
        # one. The ref file is still on disk; only the reader lost it. Raising
        # makes the reconciler skip the whole workspace, which is what its own
        # docstring already promises ("a row is only ever removed for a workspace
        # whose refs were read successfully").
        raise GitCommitError(f"broken ref in {repo_dir}: {proc.stderr.strip()}")
    return [ln.strip().removeprefix(_HEADS_PREFIX) for ln in proc.stdout.splitlines() if ln.strip()]


def branch_exists(repo_dir: Path, name: str) -> bool:
    """Whether a LOCAL branch of this name exists. Uses the full refs/heads/ path
    so a tag or a commit-ish that happens to share the name cannot answer yes.

    Only exit **1** means "no such branch". Measured 2026-08-29: `show-ref
    --verify --quiet` exits 0 when the ref exists, 1 when it does not, and **128**
    for a fatal condition (not a repository, corruption, permissions). Collapsing
    128 to False would report a broken repo as "branch not found"."""
    proc = _git(repo_dir, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitCommitError(
        f"cannot read refs in {repo_dir} (git exit {proc.returncode}): {proc.stderr.strip()}"
    )


def is_valid_branch_name(name: str) -> bool:
    """Delegated to `git check-ref-format --branch`, which owns these rules.

    A hand-rolled check that accepts a name git rejects produces a half-created
    branch; one that rejects a name git accepts is an unexplainable refusal.
    This runs without a repository — check-ref-format reads none.

    The exit code ALONE is not enough. `--branch` also EXPANDS branch shorthand
    and prints the result, so it answers "yes" for a name that means something
    else. Measured 2026-08-31 in a repo whose previous branch was `other`:

        git check-ref-format --branch '@{-1}'  ->  rc=0, prints "other"
        git branch '@{-1}'                     ->  "a branch named 'other'
                                                    already exists"

    So a user could POST `@{-1}`, pass validation, and have git act on a
    DIFFERENT branch than the one the `branches` row records — the row/ref
    divergence this whole entity exists to prevent. Requiring the output to equal
    the input closes it while keeping every git-branch-specific rule
    (`--branch` alone rejects `HEAD` and `-`; the bare form accepts `HEAD`, so
    neither is right on its own).
    """
    if not name or name != name.strip():
        return False
    proc = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=_git_env(),
    )
    return proc.returncode == 0 and proc.stdout.strip() == name


def has_commits(repo_dir: Path) -> bool:
    """Whether HEAD resolves to a commit.

    `current_branch` cannot answer this: `symbolic-ref --short HEAD` succeeds on
    an UNBORN repo and returns the branch name the first commit will land on.
    `rev-parse --verify HEAD` is the one that exits non-zero there.
    """
    proc = _git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD")
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitCommitError(
        f"cannot resolve HEAD in {repo_dir} (git exit {proc.returncode}): {proc.stderr.strip()}"
    )


def create_branch(repo_dir: Path, name: str) -> None:
    """Create *name* at the current HEAD. Does NOT move HEAD.

    Fails on a repo with no commits — `git branch <name> HEAD` exits 128 with
    "not a valid object name: 'HEAD'" — which is the correct refusal for a
    workspace still in setup.
    """
    proc = _git(repo_dir, "branch", name, "HEAD")
    if proc.returncode != 0:
        raise GitCommitError(f"cannot create branch {name!r}: {proc.stderr.strip()}")


def delete_branch(repo_dir: Path, name: str) -> None:
    """Delete *name*, FORCE (`-D`).

    `-d` refuses to delete a branch whose commits are unmerged — which is
    precisely the branch a user throws away, and a refusal that could not be
    explained in the UI. git still refuses to delete the CHECKED-OUT branch, and
    that refusal is kept as the floor beneath the app's own check.
    """
    proc = _git(repo_dir, "branch", "-D", name)
    if proc.returncode != 0:
        raise GitCommitError(f"cannot delete branch {name!r}: {proc.stderr.strip()}")


def checkout_branch(repo_dir: Path, name: str) -> None:
    """Move HEAD to *name*. The working tree follows."""
    proc = _git(repo_dir, "checkout", "-q", name)
    if proc.returncode != 0:
        raise GitCommitError(f"cannot check out branch {name!r}: {proc.stderr.strip()}")


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


@dataclass(frozen=True)
class CommitInfo:
    """One entry of a workspace repository's history."""

    sha: str
    authored_at: datetime
    subject: str
    #: Parent shas, first-parent first. Empty for a root commit, and for the
    #: OLDEST returned commit when the history was truncated -- in both cases the
    #: graph simply has no edge to draw downward, which is honest.
    parents: tuple[str, ...] = ()


# NUL framing. Commit subjects are user-controlled, and git stores ASCII control
# bytes in them verbatim — RS (0x1e) and US (0x1f) included, verified against a
# real `git commit`. Framing on either lets a crafted subject split one commit
# into two rows or shift every field one place left. NUL is the ONLY byte git
# refuses ("error: a NUL byte in commit log message not allowed"), so it is the
# only safe separator. `%x00` emits it from the format string, so no module
# constant is needed — do not reintroduce one for a byte used inline once.


# A commit subject carries a user-controlled section heading
# (editing/save.py:275: "user {artifact} edit: {heading} (user:{user_id})") and
# nothing bounds a heading's length on the way in. Counting commits is therefore
# not a bound on BYTES: `capture_output=True` buffers the whole stream before we
# ever look at it.
#
# How large can one subject actually get? This app writes messages with
# `git commit -m` (gitio.py:195), so argv caps a subject it creates at Linux's
# MAX_ARG_STRLEN, ~128 KiB — about 25 MiB across 201 records, not the gigabytes
# the request-body limit alone would suggest. But an imported or externally
# built repository has no such ceiling, and imported repositories are exactly
# the population this reader exists to inspect.
#
# Truncating in the format string is not an option: git's `%<(N,trunc)` PADS
# shorter subjects to width with spaces (verified), corrupting every ordinary
# subject. So the bound lives here, applied as the stream is consumed:
#
#   * each FIELD keeps at most _MAX_FIELD_BYTES; the rest is discarded as it
#     arrives, so one hostile subject cannot starve the other records the way a
#     single whole-output budget would, and
#   * a total scan budget backstops the discard loop, so a pathological stream
#     is refused outright rather than read forever. It surfaces as
#     GitCommitError, which the inspect route already renders as "the history
#     could not be read" — a state, not a crash.
_MAX_SUBJECT_CHARS = 500
_SUBJECT_TRUNCATION_MARKER = "… [truncated]"
# x4: a UTF-8 character is up to 4 bytes, so this is the byte budget that can
# never lose a character we intend to keep.
_MAX_FIELD_BYTES = _MAX_SUBJECT_CHARS * 4
_MAX_SCAN_BYTES = 32 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def _read_capped_fields(stream, *, max_fields: int, repo_dir: Path, deadline: float) -> list[bytes]:
    """Read NUL-separated fields, keeping at most _MAX_FIELD_BYTES of each.

    Bytes beyond a field's cap are consumed and dropped rather than buffered, so
    peak memory is max_fields * _MAX_FIELD_BYTES regardless of what git emits.
    Stops at max_fields, at EOF, at `deadline`, or by raising GitCommitError once
    the total scanned exceeds _MAX_SCAN_BYTES.

    **`deadline` is why this reads the fd directly.** Every other git call in
    this module hands `timeout=_GIT_TIMEOUT_SECONDS` to `subprocess.run`, which
    covers the whole call. `list_commits` uses Popen instead — to cap bytes — and
    that traded the timeout away: `proc.wait(timeout=...)` sits BELOW this read
    and is unreachable until stdout closes, so a git that stalls with the pipe
    open blocked forever. Executed against a stub git that emits nothing and
    sleeps: still running at 45s against a documented 30s ceiling.

    `select` reports readiness with a per-wait budget; `os.read` then takes only
    what is already there. A buffered `stream.read(n)` cannot be used alongside
    it — that loops until it has n bytes and would block past the deadline even
    once select said ready.
    """
    fields: list[bytes] = []
    current = bytearray()
    scanned = 0

    # An in-memory stream — the byte-cap tests below this module — has no OS
    # descriptor and cannot block, so it needs none of the deadline machinery,
    # and requiring one would make the cap logic untestable without a
    # subprocess. `deadline` stays a REQUIRED argument regardless, so no future
    # caller can quietly reintroduce the unbounded read this fixes.
    try:
        fd: int | None = stream.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        fd = None

    def _next_chunk() -> bytes:
        if fd is None:
            return stream.read(_READ_CHUNK_BYTES)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise GitCommitError(f"git log for {repo_dir} timed out after {_GIT_TIMEOUT_SECONDS}s")
        return os.read(fd, _READ_CHUNK_BYTES)

    def _close_field() -> None:
        fields.append(bytes(current))
        current.clear()

    while len(fields) < max_fields:
        chunk = _next_chunk()
        if not chunk:
            break
        scanned += len(chunk)
        if scanned > _MAX_SCAN_BYTES:
            raise GitCommitError(
                f"git log output for {repo_dir} exceeded {_MAX_SCAN_BYTES} bytes; "
                f"refusing to read further"
            )
        pos = 0
        while len(fields) < max_fields:
            nul = chunk.find(b"\x00", pos)
            segment = chunk[pos:] if nul == -1 else chunk[pos:nul]
            keep = _MAX_FIELD_BYTES - len(current)
            if keep > 0:
                current += segment[:keep]
            if nul == -1:
                break
            _close_field()
            pos = nul + 1
    if current and len(fields) < max_fields:
        # No trailing NUL (git was killed, or the stream ended mid-field).
        _close_field()
    return fields


#: A sha is hex and exactly as long as the sha the same record reported for
#: itself -- not a hard-coded 40, so a sha-256 repository needs no code change.
#: Same reasoning as ports/shas.py's 7-64 range.
_HEX_RE = re.compile(r"[0-9a-fA-F]+")


def _parse_parents(field: str, *, sha_len: int) -> tuple[str, ...]:
    """The `%P` field, with anything that is not a whole sha discarded.

    `_read_capped_fields` keeps at most _MAX_FIELD_BYTES of EVERY field, so a
    parent list longer than about 48 shas is cut, and the cut can land mid-sha.
    A bare `.split()` would hand that fragment on as a real parent, and the
    version graph would open a lane for a commit that does not exist and never
    close it.

    Dropping is the conservative direction: a missing edge draws nothing, an
    invented edge draws a line to nowhere.
    """
    raw = field.split()
    kept = tuple(p for p in raw if len(p) == sha_len and _HEX_RE.fullmatch(p))
    if len(kept) != len(raw):
        logger.warning(
            "gitio.dropped_malformed_parents",
            extra={
                "event": "gitio_dropped_malformed_parents",
                "dropped": len(raw) - len(kept),
            },
        )
    return kept


def list_commits(
    repo_dir: Path,
    *,
    limit: int,
    pathspec: str | None = None,
    all_branches: bool = False,
) -> tuple[list[CommitInfo], bool]:
    """Return (commits newest-first, truncated).

    Bounded at the source twice over: asks git for `limit + 1` and reports
    `truncated` when that extra row comes back, AND stops reading git's output
    after a byte cap derived from `limit`, so a single hostile commit subject
    cannot make one call buffer gigabytes. Hitting the byte cap also reports
    `truncated` — more history exists than was returned, which is exactly what
    the flag means.

    Individual subjects are truncated to `_MAX_SUBJECT_CHARS` with a visible
    marker.

    `all_branches` widens the query from HEAD's history to every local branch
    plus HEAD (so a detached HEAD's own commits are still included), in
    topological order. It does NOT widen which FILES are considered -- `pathspec`
    still applies.

    `pathspec` scopes the history to commits touching that path. It is passed
    after a literal `--` so a filename can never be read as an option, and it is
    applied INSIDE `git log` — scoping the returned list instead would let
    unrelated commits consume `--max-count` and hide older matching commits.
    Omitted, the history is unscoped, exactly as before.

    Raises GitCommitError if the repository is missing, is not a repository, or
    has no commits (an unborn HEAD).
    """
    argv = [
        "git",
        "log",
        f"--max-count={limit + 1}",
        # ALWAYS passed, pathspec or not. Under a pathspec git REWRITES parent
        # links, and %P only reports the rewritten parents when --parents is
        # given -- without it the graph draws edges to commits that were
        # filtered out. Measured; pinned by
        # test_pathspec_history_rewrites_the_parent_link.
        "--parents",
        # -z terminates each commit with NUL; %x00 separates the four
        # fields within one. Both are the same byte, so the stream is a
        # flat run of NUL-separated fields, parsed four at a time below.
        "-z",
        "--format=%H%x00%P%x00%aI%x00%s",
    ]
    if all_branches:
        # `--branches` is refs/heads ONLY -- no tags, no remotes, which is what
        # the version pane wants. The bare `HEAD` is NOT redundant: measured,
        # `--branches` alone OMITS a commit made on a detached HEAD, so the pane
        # would show a history missing its own tip.
        #
        # `--topo-order` guarantees children before parents, which the lane
        # algorithm depends on. Applied only here, so the default path stays
        # byte-identical for admin_retention.
        argv += ["--branches", "HEAD", "--topo-order"]
    if pathspec is not None:
        # After `--`, so a filename can never be parsed as an option. git also
        # enables history simplification here, which is what makes the count
        # honest: `--max-count` now counts MATCHING commits.
        argv += ["--", pathspec]
    # Four fields per record, and git emits a trailing NUL after the last one.
    max_fields = 4 * (limit + 1)
    try:
        with subprocess.Popen(
            argv,
            cwd=repo_dir,
            env=_list_commits_env(repo_dir),
            stdout=subprocess.PIPE,  # bytes (NO text=True) so WE control decoding
            stderr=subprocess.PIPE,
        ) as proc:
            try:
                raw_fields = _read_capped_fields(
                    proc.stdout,
                    max_fields=max_fields,
                    repo_dir=repo_dir,
                    # One deadline for the whole call, not per-read: N slow reads
                    # under a per-read budget would still add up to forever.
                    deadline=time.monotonic() + _GIT_TIMEOUT_SECONDS,
                )
                complete = len(raw_fields) < max_fields
                if complete:
                    stderr_bytes = proc.stderr.read() if proc.stderr else b""
                    if proc.wait(timeout=_GIT_TIMEOUT_SECONDS) != 0:
                        raise subprocess.CalledProcessError(
                            proc.returncode, argv, b"", stderr_bytes
                        )
                else:
                    # We stopped reading at max_fields; git may still be writing.
                    # Its exit status is meaningless after a kill, so don't check
                    # it — we already have every record we asked for.
                    proc.kill()
            except BaseException:
                proc.kill()
                raise
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        _log_if_timeout(e)
        # OSError (FileNotFoundError on a missing repo_dir — the case this
        # function's contract names) carries no .stderr, so falling back to the
        # exception keeps the OS-level text in the message rather than emitting
        # a bare "cannot list commits in /path: ". Mirrors current_head.
        raw_err = getattr(e, "stderr", b"") or b""
        if isinstance(raw_err, bytes):
            raw_err = raw_err.decode("utf-8", errors="replace")
        detail = raw_err.strip() or str(e)
        raise GitCommitError(f"cannot list commits in {repo_dir}: {detail}") from e

    # errors="replace", mirroring read_blob: a commit subject is not guaranteed
    # valid UTF-8 (git stores message bytes verbatim, and an imported or damaged
    # repository is exactly the population this reader exists to inspect). With
    # text=True the decode happens inside subprocess.run and raises
    # UnicodeDecodeError, which is neither OSError nor GitCommitError — it would
    # escape every caller's except clause and surface as a 500 instead of the
    # deliberate "no readable repository" state.
    #
    # A field capped mid-character yields one U+FFFD at the seam; harmless, and
    # the subject is about to be marked truncated anyway.
    fields = [f.decode("utf-8", errors="replace") for f in raw_fields]

    # git -z emits a trailing NUL, so a fully-read stream ends in an empty tail.
    # Drop ONLY that tail — do not filter on truthiness, which would silently
    # swallow an empty subject and shift every field after it.
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 4 != 0:
        raise GitCommitError(
            f"malformed git log output for {repo_dir}: {len(fields)} NUL-separated "
            f"fields is not a multiple of 4"
        )
    records = [tuple(fields[i : i + 4]) for i in range(0, len(fields), 4)]
    truncated = len(records) > limit
    commits: list[CommitInfo] = []
    for sha, parents_field, authored, subject in records[:limit]:
        try:
            # %aI is strict ISO-8601 with an offset; fromisoformat handles it on
            # 3.12. A parse failure means the framing shifted — fail loudly, but
            # as GitCommitError: a bare ValueError escapes every caller's except
            # clause and becomes a 500 rather than a handled state.
            authored_at = datetime.fromisoformat(authored)
        except ValueError as e:
            raise GitCommitError(f"unparseable author date in {repo_dir}: {authored!r}") from e
        if len(subject) > _MAX_SUBJECT_CHARS:
            # Marked, not silently clipped: an admin reading a retention record
            # must be able to tell a short subject from a shortened one.
            subject = subject[:_MAX_SUBJECT_CHARS] + _SUBJECT_TRUNCATION_MARKER
        commits.append(
            CommitInfo(
                sha=sha,
                authored_at=authored_at,
                subject=subject,
                parents=_parse_parents(parents_field, sha_len=len(sha)),
            )
        )
    return commits, truncated


def _list_commits_env(repo_dir: Path) -> dict[str, str]:
    """`_git_env()` plus a discovery ceiling.

    `_git_env()` (gitio.py:34) sets only author/committer identity. It does NOT
    pin repository discovery, so `git log` in a plain directory walks up and
    reports an enclosing repository's history. `current_head` (gitio.py:290)
    already guards this; `list_commits` reads the same directories and needs the
    same guard, or a workspace whose repo was never created shows an admin the
    project's own commits.
    """
    return dict(_git_env(), GIT_CEILING_DIRECTORIES=str(Path(repo_dir).resolve().parent))
