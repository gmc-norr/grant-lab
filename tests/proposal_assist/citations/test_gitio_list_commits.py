"""list_commits: bounded, newest-first, truncation detectable.

Inspection (retention Slice B) shows an admin the history of a deleted
workspace. History grows with every save, so the read must be bounded at the
source rather than trimmed after the fact.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from call_manager.proposal_assist.citations import gitio
from call_manager.proposal_assist.citations.gitio import (
    _MAX_SUBJECT_CHARS,
    _SUBJECT_TRUNCATION_MARKER,
    GitCommitError,
    list_commits,
)


def _repo(tmp_path: Path, n_commits: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    for i in range(n_commits):
        (repo / "proposal.md").write_text(f"# draft {i}\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", f"commit {i}"],
            cwd=repo,
            check=True,
        )
    return repo


def test_returns_commits_newest_first(tmp_path):
    repo = _repo(tmp_path, 3)
    commits, truncated = list_commits(repo, limit=10)
    assert [c.subject for c in commits] == ["commit 2", "commit 1", "commit 0"]
    assert truncated is False


def test_truncates_at_the_limit_and_says_so(tmp_path):
    repo = _repo(tmp_path, 5)
    commits, truncated = list_commits(repo, limit=2)
    assert [c.subject for c in commits] == ["commit 4", "commit 3"]
    assert truncated is True, (
        "a caller that cannot tell a capped list from a complete one will "
        "present a partial history as the whole history"
    )


def test_exactly_at_the_limit_is_not_truncated(tmp_path):
    """The off-by-one that matters: 2 commits with limit=2 is complete, not truncated."""
    repo = _repo(tmp_path, 2)
    commits, truncated = list_commits(repo, limit=2)
    assert len(commits) == 2
    assert truncated is False


def test_missing_repository_raises(tmp_path):
    with pytest.raises(GitCommitError):
        list_commits(tmp_path / "nope", limit=10)


def test_repository_with_no_commits_raises(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    with pytest.raises(GitCommitError):
        list_commits(repo, limit=10)


HOSTILE = "fix: a|b|c pipes \x1f US \x1e RS \t tab and \\n literal"


def test_a_hostile_subject_round_trips_exactly(tmp_path):
    """Commit subjects are user-controlled and git stores control bytes verbatim
    — verified: `git commit -m $'a\\x1eb'` succeeds and `%s` emits the 0x1e back.
    So ASCII RS/US framing is forgeable: a subject containing the record
    separator splits one commit into two rows, and one containing the field
    separator shifts every field.

    NUL is the one byte git refuses ("error: a NUL byte in commit log message
    not allowed"), which is why the implementation frames on it.

    Exact equality, and no `or` — an assertion with an alternative branch passes
    for whichever branch happens to hold and can never fail."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    (repo / "proposal.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", HOSTILE],
        cwd=repo,
        check=True,
    )
    commits, truncated = list_commits(repo, limit=10)
    assert len(commits) == 1, "a forged separator split one commit into several rows"
    assert commits[0].subject == HOSTILE
    assert truncated is False


def test_a_hostile_subject_does_not_corrupt_its_neighbours(tmp_path):
    """The failure mode framing exists to prevent: injection in commit 2 of 3
    must not shift commit 1's or commit 3's fields."""
    repo = _repo(tmp_path, 1)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", HOSTILE],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "after"],
        cwd=repo,
        check=True,
    )
    commits, _ = list_commits(repo, limit=10)
    assert [c.subject for c in commits] == ["after", HOSTILE, "commit 0"]
    assert all(len(c.sha) == 40 for c in commits), "a shifted field lands in sha"


def test_a_plain_directory_does_not_inherit_an_enclosing_repository(tmp_path):
    """`repo_dir` may exist without being a repository — a workspace deleted
    before setup ever ran `git init`. Without GIT_CEILING_DIRECTORIES git walks
    up and reports the ENCLOSING repository's history, so an admin would be
    shown this project's commits as if they were the workspace's.

    Mirrors the ceiling that `current_head` already sets (gitio.py:290)."""
    outer = tmp_path / "outer"
    (outer / "inner").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=outer, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "enclosing repo commit"],
        cwd=outer,
        check=True,
    )
    with pytest.raises(GitCommitError):
        list_commits(outer / "inner", limit=10)

def test_a_non_utf8_subject_does_not_escape_as_unicodedecodeerror(tmp_path):
    """git stores commit-message bytes verbatim, and an imported or damaged
    repository is exactly the population the admin retention surface exists to
    inspect.

    With `text=True` the decode happens inside subprocess.run and raises
    UnicodeDecodeError, which is neither OSError nor GitCommitError. It would
    escape every caller's except clause and surface as a 500, where the spec
    requires a deliberate "no readable repository" state. Decoding here with
    errors="replace" mirrors read_blob, which carries the same guarantee.

    The commit is written as a raw object with an `encoding UTF-8` header over a
    body that is not UTF-8. That combination matters: with no header (or a
    truthful one) git transcodes on output and Python never sees the bad bytes,
    so a test built with `commit-tree` alone passes whether the fix is present
    or not. Verified: this construction does produce undecodable stdout.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    tree = subprocess.run(
        ["git", "write-tree"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    raw = (
        f"tree {tree}\n".encode()
        + b"author t <t@t> 1700000000 +0000\n"
        + b"committer t <t@t> 1700000000 +0000\n"
        + b"encoding UTF-8\n"
        + b"\n\xff\xfe raw invalid\n"
    )
    sha = (
        subprocess.run(
            ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=repo,
            input=raw,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(["git", "update-ref", "HEAD", sha], cwd=repo, check=True)

    commits, truncated = list_commits(repo, limit=10)

    assert len(commits) == 1
    assert commits[0].sha == sha
    assert truncated is False
    # U+FFFD replacement chars, not a raised exception.
    assert "\ufffd" in commits[0].subject
    assert "raw invalid" in commits[0].subject


def test_an_unparseable_author_date_raises_gitcommiterror_not_valueerror(tmp_path, monkeypatch):
    """Same reasoning as the decode test one layer down: datetime.fromisoformat
    raises ValueError, which is neither OSError nor GitCommitError and so
    escapes callers' except clauses. Framing can only shift on a malformed
    stream, but when it does the caller must still see the handled type."""
    from call_manager.proposal_assist.citations import gitio

    repo = _repo(tmp_path, 1)
    monkeypatch.setattr(
        gitio,
        "_read_capped_fields",
        # sha, PARENTS (empty -- a root commit), date, subject. Four fields
        # since list_commits started reading %P; three would now be caught by
        # the "not a multiple of 4" guard instead, which is a different failure
        # from the one this test is about.
        lambda stream, **kw: [b"a" * 40, b"", b"not-a-date", b"subject"],
    )

    with pytest.raises(GitCommitError, match="unparseable author date"):
        list_commits(repo, limit=10)


def test_an_oversized_subject_is_truncated_and_cannot_blow_up_the_read(tmp_path):
    """A commit subject carries a user-controlled section heading
    (editing/save.py:275) with no length bound on the way in; the only ceiling
    is the 64 MiB request-body cap. Counting commits does not bound bytes, so
    without a byte cap one inspection of a hostile repository would buffer
    gigabytes before decoding and the JSON copy.

    Verifies both halves: individual subjects are truncated, and the whole read
    stays near the cap rather than near the repository's real size."""
    repo = _repo(tmp_path, 1)
    # -F, not -m: a message this long as an argv element exceeds Linux's
    # MAX_ARG_STRLEN (~128 KiB), so `git commit -m` — which is how this app
    # writes commits (gitio.py:195) — cannot itself produce one. A repository
    # imported or built by other tools can, and imported repositories are part
    # of the population this reader exists to inspect.
    msg = repo / "msg.txt"
    msg.write_text("edit: " + "H" * (2 * 1024 * 1024), encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-F", str(msg)],
        cwd=repo,
        check=True,
    )

    commits, truncated = list_commits(repo, limit=10)

    assert commits, "the oversized subject must not empty the result"
    biggest = max(len(c.subject) for c in commits)
    assert biggest <= _MAX_SUBJECT_CHARS + len(_SUBJECT_TRUNCATION_MARKER)
    assert commits[0].subject.endswith(_SUBJECT_TRUNCATION_MARKER)
    assert commits[0].subject.startswith("edit: HHH")
    # Ordinary subjects are NOT padded or marked — git's own %<(N,trunc) pads
    # to width, which is why the truncation happens here instead.
    assert commits[-1].subject == "commit 0"
    assert len(commits) == 2, "the oversized subject must not starve its neighbour"
    assert truncated is False, (
        "the repository has 2 commits and the limit is 10; an oversized subject "
        "is not a truncated history"
    )


def test_a_repository_of_oversized_subjects_reports_truncated_not_everything(tmp_path):
    """When the byte cap IS reached the caller must learn that more history
    exists, exactly as it would from the count cap — otherwise a partial list
    is presented as the whole one."""
    repo = _repo(tmp_path, 1)
    msg = repo / "msg.txt"
    for i in range(6):
        msg.write_text(f"e{i}: " + "H" * (600 * 1024), encoding="utf-8")
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "--allow-empty", "-F", str(msg)],
            cwd=repo,
            check=True,
        )

    commits, truncated = list_commits(repo, limit=3)

    cap = _MAX_SUBJECT_CHARS + len(_SUBJECT_TRUNCATION_MARKER)
    # Every requested record still comes back. Capping per FIELD rather than
    # over the whole stream is what makes this true: a single whole-output
    # budget would be spent by the first hostile subject, and the admin would
    # silently see one commit instead of three.
    assert len(commits) == 3
    assert truncated is True, "more history exists than was returned"
    assert all(len(c.subject) <= cap for c in commits)
    assert all(len(c.sha) == 40 for c in commits), "a half-read record leaked into sha"
    assert [c.subject[:2] for c in commits] == ["e5", "e4", "e3"], "records shifted"


def test_the_reader_caps_each_field_before_it_is_buffered():
    """The point of P1c is the MEMORY bound, and the subject-truncation tests
    above do NOT pin it: they assert on the final CommitInfo, which is clipped to
    _MAX_SUBJECT_CHARS in Python afterwards either way. Raising _MAX_FIELD_BYTES
    to 64 MiB leaves every one of them green — verified — while restoring the
    unbounded buffering the bound exists to prevent.

    So assert the reader directly: what comes back per field is capped, and the
    oversized remainder was consumed and dropped rather than accumulated.
    """
    import io as _io

    from call_manager.proposal_assist.citations.gitio import (
        _MAX_FIELD_BYTES,
        _read_capped_fields,
    )

    oversized = b"X" * (8 * 1024 * 1024)
    stream = _io.BytesIO(b"sha\x00date\x00" + oversized + b"\x00")

    fields = _read_capped_fields(
        stream,
        max_fields=3,
        repo_dir=Path("/nonexistent"),
        # Required, so no caller can quietly go unbounded again. A BytesIO
        # has no descriptor and cannot block, so it is never consulted.
        deadline=time.monotonic() + 60,
    )

    assert [f[:4] for f in fields[:2]] == [b"sha", b"date"]
    assert len(fields[2]) == _MAX_FIELD_BYTES, (
        "the oversized field was buffered whole instead of being capped as it "
        "streamed — one hostile commit subject can then size the read"
    )
    assert sum(len(f) for f in fields) <= 3 * _MAX_FIELD_BYTES


def test_the_reader_refuses_a_pathological_stream_rather_than_reading_forever():
    """The per-field cap drops bytes but still has to scan to the next NUL. The
    total scan budget is the backstop, and it surfaces as GitCommitError, which
    the inspect route renders as "the history could not be read" — a state, not
    a crash."""
    import io as _io

    from call_manager.proposal_assist.citations.gitio import _MAX_SCAN_BYTES, _read_capped_fields

    stream = _io.BytesIO(b"X" * (_MAX_SCAN_BYTES + 1024))

    with pytest.raises(GitCommitError, match="exceeded"):
        _read_capped_fields(
            stream,
            max_fields=3,
            repo_dir=Path("/nonexistent"),
            # Required, so no caller can quietly go unbounded again. A BytesIO
            # has no descriptor and cannot block, so it is never consulted.
            deadline=time.monotonic() + 60,
        )


def _repo_two_files(tmp_path: Path) -> Path:
    """A repo whose NEWEST commits touch only proposal-spec.md.

    This shape is the whole point: with the pathspec applied AFTER git has
    already capped at --max-count, a small limit returns only spec commits and
    the draft's history vanishes. A fixture whose commits all touch both files
    cannot fail on that bug.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)

    def commit(name: str, body: str, msg: str) -> None:
        (repo / name).write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", msg],
            cwd=repo,
            check=True,
        )

    commit("proposal.md", "# draft 0\n", "draft 0")
    commit("proposal.md", "# draft 1\n", "draft 1")
    for i in range(5):
        commit("proposal-spec.md", f"# spec {i}\n", f"spec {i}")
    return repo


def test_pathspec_scopes_to_one_file(tmp_path):
    repo = _repo_two_files(tmp_path)
    commits, truncated = list_commits(repo, limit=10, pathspec="proposal.md")
    assert [c.subject for c in commits] == ["draft 1", "draft 0"]
    assert truncated is False


def test_pathspec_is_applied_before_the_limit(tmp_path):
    """THE gate. The 5 newest commits touch only the spec. With a limit of 3 and
    the scope applied inside git log, the draft's two commits still come back.
    Filtering list_commits' RESULT instead would return an empty list."""
    repo = _repo_two_files(tmp_path)
    commits, truncated = list_commits(repo, limit=3, pathspec="proposal.md")
    assert [c.subject for c in commits] == ["draft 1", "draft 0"]
    assert truncated is False


def test_pathspec_matching_nothing_is_empty_not_an_error(tmp_path):
    repo = _repo_two_files(tmp_path)
    commits, truncated = list_commits(repo, limit=10, pathspec="does-not-exist.md")
    assert commits == []
    assert truncated is False


def test_omitting_pathspec_keeps_the_old_unscoped_behaviour(tmp_path):
    repo = _repo_two_files(tmp_path)
    commits, _ = list_commits(repo, limit=10)
    assert len(commits) == 7


def test_pathspec_still_reports_truncation(tmp_path):
    repo = _repo_two_files(tmp_path)
    commits, truncated = list_commits(repo, limit=1, pathspec="proposal-spec.md")
    assert [c.subject for c in commits] == ["spec 4"]
    assert truncated is True


def _stalling_git(tmp_path: Path) -> Path:
    """A `git` that starts, emits nothing, and never exits.

    Stands in for the real stall this bounds: a filesystem that stops answering
    (NFS, a failing disk) while git holds the pipe open. Every OTHER git call in
    gitio.py passes `timeout=_GIT_TIMEOUT_SECONDS` to `subprocess.run`, which
    covers the whole call. `list_commits` alone uses Popen — to cap bytes — and
    reading its stdout carried no deadline, so `proc.wait(timeout=...)` below the
    read was unreachable until stdout closed.
    """
    bindir = tmp_path / "stallbin"
    bindir.mkdir()
    fake = bindir / "git"
    fake.write_text("#!/bin/bash\nsleep 3600\n", encoding="utf-8")
    fake.chmod(0o755)
    return bindir


def test_a_stalled_git_raises_instead_of_hanging(tmp_path, monkeypatch):
    """Executed before the fix: still running at 45s against a 30s contract.

    This matters more than it looks. The history routes are sync `def`, so
    Starlette runs them in its threadpool — ~40 threads. A request that never
    returns holds one forever, and enough of them stop the whole app serving
    ANY route, not just this one.
    """
    repo = _repo(tmp_path, 1)
    monkeypatch.setattr(gitio, "_GIT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setenv("PATH", f"{_stalling_git(tmp_path)}:{os.environ['PATH']}")

    started = time.monotonic()
    with pytest.raises(GitCommitError) as exc:
        list_commits(repo, limit=10)
    elapsed = time.monotonic() - started

    assert "timed out" in str(exc.value).lower(), str(exc.value)
    # Bounded by the deadline, with slack for process teardown — and nowhere
    # near the 3600s the stub would otherwise sleep.
    assert elapsed < 15, f"took {elapsed:.1f}s; the deadline did not fire"


def test_a_stalled_git_is_killed_and_reaped(tmp_path, monkeypatch):
    """The stub sleeps for an hour. If the deadline fires but the child is not
    killed and reaped, one process outlives every stalled request.

    Asserts on the actual Popen object rather than counting processes on the box:
    `poll()` returning non-None is exactly "this child is dead and reaped", and
    it cannot be confused by anything else running.
    """
    repo = _repo(tmp_path, 1)
    monkeypatch.setattr(gitio, "_GIT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setenv("PATH", f"{_stalling_git(tmp_path)}:{os.environ['PATH']}")

    spawned = []
    real_popen = subprocess.Popen

    def _spy(*a, **kw):
        proc = real_popen(*a, **kw)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(gitio.subprocess, "Popen", _spy)

    with pytest.raises(GitCommitError):
        list_commits(repo, limit=10)

    assert spawned, "no git subprocess was started"
    assert spawned[0].poll() is not None, (
        "the stalled git was left running: the deadline fired but the child was "
        "never killed and reaped"
    )


# --- parents (version-graph lanes) ------------------------------------------


def _empty_repo(tmp_path: Path) -> Path:
    """Like `_repo(tmp_path, 0)` but with a named initial branch, so the
    branch-aware tests below have something deterministic to check out from."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)
    return repo


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
    )


def _commit(repo: Path, name: str, body: str, message: str) -> str:
    """Commit *body* to *name* and return the resulting full sha."""
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_a_root_commit_has_no_parents(tmp_path):
    repo = _empty_repo(tmp_path)
    _commit(repo, "proposal.md", "v1", "c1")
    commits, _ = list_commits(repo, limit=10)
    assert commits[0].parents == ()


def test_a_child_commit_names_its_parent(tmp_path):
    repo = _empty_repo(tmp_path)
    first = _commit(repo, "proposal.md", "v1", "c1")
    _commit(repo, "proposal.md", "v2", "c2")
    commits, _ = list_commits(repo, limit=10)
    assert commits[0].parents == (first,)


def test_a_merge_commit_names_both_parents(tmp_path):
    repo = _empty_repo(tmp_path)
    _commit(repo, "proposal.md", "a\n", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    feature = _commit(repo, "proposal.md", "a\nF\n", "feature edit")
    _git(repo, "checkout", "-q", "main")
    main = _commit(repo, "proposal.md", "M\na\n", "main edit")
    _git(repo, "merge", "--no-ff", "-q", "-m", "merge", "-X", "ours", "feature")
    commits, _ = list_commits(repo, limit=10)
    merge = commits[0]
    assert merge.subject == "merge"
    assert set(merge.parents) == {main, feature}
    # ORDER matters to the lane algorithm: the first parent inherits the lane.
    assert merge.parents[0] == main


def test_pathspec_history_rewrites_the_parent_link(tmp_path):
    """THE --parents gate. Spec section 3.2.

    Oldest first: c1 touches the artifact, c2 touches something else, c3 touches
    the artifact. Under a pathspec git returns only c1 and c3, and REWRITES the
    NEWER one's parent to point at the older MATCHING commit.

    Without `--parents` in the argv, git reports c3's parent as c2 -- a commit
    that is not in the returned list, so the graph would draw an edge to a row
    that is not on screen. Delete `--parents` from `list_commits` and this test
    must go red.
    """
    repo = _empty_repo(tmp_path)
    c1 = _commit(repo, "proposal.md", "v1", "c1 artifact")
    _commit(repo, "other.md", "o1", "c2 unrelated")
    _commit(repo, "proposal.md", "v2", "c3 artifact")

    commits, _ = list_commits(repo, limit=10, pathspec="proposal.md")
    assert [c.subject for c in commits] == ["c3 artifact", "c1 artifact"]
    assert commits[0].parents == (c1,), (
        "c3's parent must be rewritten to c1; got a parent that is not in the list"
    )


def test_a_malformed_parent_token_is_dropped_not_half_parsed(tmp_path, caplog):
    """Guards the _MAX_FIELD_BYTES cut (2000 bytes ~ 48 parents).

    A cut lands mid-sha, and a partial sha becomes a lane waiting for a commit
    that will never arrive. Exercised through the parse helper rather than by
    building a 49-parent octopus, which git makes needlessly painful.
    """
    import logging

    from call_manager.proposal_assist.citations.gitio import _parse_parents

    good = "a" * 40
    assert _parse_parents("", sha_len=40) == ()
    assert _parse_parents(f"{good} {'Z' * 40}", sha_len=40) == (good,)

    # And it must SAY SO. Without this assertion, deleting the logger.warning
    # leaves the test green -- the silent drop is the failure mode that makes a
    # damaged repo look merely uninteresting.
    #
    # NOT `... in r.message or r.msg` -- `r.msg` is a non-empty string, so the
    # `or` short-circuits and the assertion passes on an empty log.
    with caplog.at_level(logging.WARNING):
        assert _parse_parents(f"{good} {'b' * 17}", sha_len=40) == (good,)
    assert any(
        r.levelname == "WARNING" and "dropped_malformed_parents" in r.getMessage()
        for r in caplog.records
    ), "a dropped parent must be logged"


# --- all_branches ------------------------------------------------------------


def test_all_branches_returns_commits_off_the_checked_out_branch(tmp_path):
    repo = _empty_repo(tmp_path)
    _commit(repo, "proposal.md", "v1", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "proposal.md", "v2", "only on side")
    _git(repo, "checkout", "-q", "main")

    default, _ = list_commits(repo, limit=10)
    assert "only on side" not in [c.subject for c in default]

    every, _ = list_commits(repo, limit=10, all_branches=True)
    assert "only on side" in [c.subject for c in every]


def test_all_branches_still_includes_a_detached_head(tmp_path):
    """THE `HEAD` gate. Spec section 3.3.

    `git log --branches` lists refs/heads only, so a commit made on a detached
    HEAD belongs to no branch and vanishes -- the pane would show a history
    missing its own tip. `--branches HEAD` is the union. Measured.

    Delete the bare "HEAD" argument from the all_branches argv and this must go
    red.
    """
    repo = _empty_repo(tmp_path)
    _commit(repo, "proposal.md", "v1", "base")
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    _commit(repo, "proposal.md", "v2", "made while detached")

    every, _ = list_commits(repo, limit=10, all_branches=True)
    assert "made while detached" in [c.subject for c in every]


def test_all_branches_orders_children_before_parents(tmp_path):
    repo = _empty_repo(tmp_path)
    root = _commit(repo, "proposal.md", "v1", "root")
    _commit(repo, "proposal.md", "v2", "child")
    every, _ = list_commits(repo, limit=10, all_branches=True)
    assert [c.subject for c in every] == ["child", "root"]
    assert every[0].parents == (root,)
