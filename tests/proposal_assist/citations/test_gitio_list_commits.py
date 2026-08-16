"""list_commits: bounded, newest-first, truncation detectable.

Inspection (retention Slice B) shows an admin the history of a deleted
workspace. History grows with every save, so the read must be bounded at the
source rather than trimmed after the fact.
"""

import subprocess
from pathlib import Path

import pytest

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
        lambda stream, **kw: [b"a" * 40, b"not-a-date", b"subject"],
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

    fields = _read_capped_fields(stream, max_fields=3, repo_dir=Path("/nonexistent"))

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
        _read_capped_fields(stream, max_fields=3, repo_dir=Path("/nonexistent"))
