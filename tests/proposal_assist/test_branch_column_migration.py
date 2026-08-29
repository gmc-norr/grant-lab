"""The current_branch backfill reads each repo, and refuses to guess.

Uses the house fixtures: `make_workspace` (tests/proposal_assist/conftest.py:200)
and `pg_session_factory` (tests/conftest.py:69). Do not construct a Workspace by
hand — it needs an owner and, for some paths, a call.
"""

import subprocess

import pytest
from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.branch_backfill import backfill_current_branch
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_workspace

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def _repo_on(workspaces_dir, workspace_id, branch, *, commit=True, detach=False):
    d = workspace_repo_dir(workspaces_dir, workspace_id)
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch, "."], cwd=d, check=True)
    if commit:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=d, check=True, env=_GIT_ENV
        )
    if detach:
        subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    return d


def _branch_of(session_factory, wid):
    with session_factory() as s:
        return s.execute(select(Workspace.current_branch).where(Workspace.id == wid)).scalar_one()


def test_backfill_reads_the_repos_actual_branch(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo_on(tmp_path, wid, "aims-ambitious")

    with pg_session_factory() as s:
        assert backfill_current_branch(s, workspaces_dir=tmp_path) == 1
        s.commit()

    assert _branch_of(pg_session_factory, wid) == "aims-ambitious"


def test_backfill_records_an_unborn_repos_branch_rather_than_failing(pg_session_factory, tmp_path):
    # A workspace still in setup has a repo with no commits. Its branch NAME is
    # known and correct; failing here would block the whole migration on it.
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo_on(tmp_path, wid, "main", commit=False)

    with pg_session_factory() as s:
        backfill_current_branch(s, workspaces_dir=tmp_path)
        s.commit()

    assert _branch_of(pg_session_factory, wid) == "main"


def test_backfill_leaves_a_workspace_with_no_repo_at_the_default(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    # No repo directory created at all.
    with pg_session_factory() as s:
        assert backfill_current_branch(s, workspaces_dir=tmp_path) == 0
        s.commit()

    assert _branch_of(pg_session_factory, wid) == "main"


def test_backfill_RAISES_on_a_detached_head(pg_session_factory, tmp_path):
    # Nothing in the app detaches HEAD, so a detached repo means something
    # outside the app did. Writing a plausible 'main' would discard that state.
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo_on(tmp_path, wid, "main", detach=True)

    with pg_session_factory() as s, pytest.raises(GitCommitError):
        backfill_current_branch(s, workspaces_dir=tmp_path)
