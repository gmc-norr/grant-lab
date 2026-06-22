import subprocess
from pathlib import Path

from call_manager.proposal_assist.citations.gitio import (
    blob_sha,
    read_blob,
    write_and_commit_files,
)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", *args], cwd=repo, check=True)
    write_and_commit_files(repo, {"fit-check.md": "# fit\n"}, "seed")
    return repo


def test_blob_sha_and_read_blob_roundtrip(tmp_path):
    repo = _init_repo(tmp_path)
    sha = blob_sha(repo, "HEAD", "fit-check.md")
    assert len(sha) == 40
    assert read_blob(repo, sha) == "# fit\n"


def test_blob_sha_missing_path_raises(tmp_path):
    repo = _init_repo(tmp_path)
    import pytest

    from call_manager.proposal_assist.citations.gitio import GitCommitError

    with pytest.raises(GitCommitError):
        blob_sha(repo, "HEAD", "nope.md")
