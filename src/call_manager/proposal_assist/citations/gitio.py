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


@dataclass(frozen=True)
class CommitInfo:
    """One entry of a workspace repository's history."""

    sha: str
    authored_at: datetime
    subject: str


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


def _read_capped_fields(stream, *, max_fields: int, repo_dir: Path) -> list[bytes]:
    """Read NUL-separated fields, keeping at most _MAX_FIELD_BYTES of each.

    Bytes beyond a field's cap are consumed and dropped rather than buffered, so
    peak memory is max_fields * _MAX_FIELD_BYTES regardless of what git emits.
    Stops at max_fields, at EOF, or by raising GitCommitError once the total
    scanned exceeds _MAX_SCAN_BYTES.
    """
    fields: list[bytes] = []
    current = bytearray()
    scanned = 0

    def _close_field() -> None:
        fields.append(bytes(current))
        current.clear()

    while len(fields) < max_fields:
        chunk = stream.read(_READ_CHUNK_BYTES)
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


def list_commits(repo_dir: Path, *, limit: int) -> tuple[list[CommitInfo], bool]:
    """Return (commits newest-first, truncated).

    Bounded at the source twice over: asks git for `limit + 1` and reports
    `truncated` when that extra row comes back, AND stops reading git's output
    after a byte cap derived from `limit`, so a single hostile commit subject
    cannot make one call buffer gigabytes. Hitting the byte cap also reports
    `truncated` — more history exists than was returned, which is exactly what
    the flag means.

    Individual subjects are truncated to `_MAX_SUBJECT_CHARS` with a visible
    marker.

    Raises GitCommitError if the repository is missing, is not a repository, or
    has no commits (an unborn HEAD).
    """
    argv = [
        "git",
        "log",
        f"--max-count={limit + 1}",
        # -z terminates each commit with NUL; %x00 separates the three
        # fields within one. Both are the same byte, so the stream is a
        # flat run of NUL-separated fields, parsed three at a time below.
        "-z",
        "--format=%H%x00%aI%x00%s",
    ]
    # Three fields per record, and git emits a trailing NUL after the last one.
    max_fields = 3 * (limit + 1)
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
                    proc.stdout, max_fields=max_fields, repo_dir=repo_dir
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
        raise GitCommitError(
            f"cannot list commits in {repo_dir}: {detail}"
        ) from e

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
    if len(fields) % 3 != 0:
        raise GitCommitError(
            f"malformed git log output for {repo_dir}: {len(fields)} NUL-separated "
            f"fields is not a multiple of 3"
        )
    records = [tuple(fields[i : i + 3]) for i in range(0, len(fields), 3)]
    truncated = len(records) > limit
    commits: list[CommitInfo] = []
    for sha, authored, subject in records[:limit]:
        try:
            # %aI is strict ISO-8601 with an offset; fromisoformat handles it on
            # 3.12. A parse failure means the framing shifted — fail loudly, but
            # as GitCommitError: a bare ValueError escapes every caller's except
            # clause and becomes a 500 rather than a handled state.
            authored_at = datetime.fromisoformat(authored)
        except ValueError as e:
            raise GitCommitError(
                f"unparseable author date in {repo_dir}: {authored!r}"
            ) from e
        if len(subject) > _MAX_SUBJECT_CHARS:
            # Marked, not silently clipped: an admin reading a retention record
            # must be able to tell a short subject from a shortened one.
            subject = subject[:_MAX_SUBJECT_CHARS] + _SUBJECT_TRUNCATION_MARKER
        commits.append(CommitInfo(sha=sha, authored_at=authored_at, subject=subject))
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
