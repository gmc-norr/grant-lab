import os
import subprocess
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    acquire_commit_mutex,
    commit_mutex_key,
    workspace_lock_key,
)
from call_manager.proposal_assist.paths import workspace_repo_dir


def test_commit_mutex_key_differs_from_subprocess_lock_key():
    ws = uuid.uuid4()
    assert commit_mutex_key(ws) != workspace_lock_key(ws)


def test_commit_mutex_key_pinned_literal():
    # pinned: changing the derivation invalidates documented operational
    # assumptions (operators query pg_locks by this key).
    zero = uuid.UUID("00000000-0000-0000-0000-000000000000")
    assert commit_mutex_key(zero) == -5477740313260656919


def test_commit_mutex_serializes_two_sessions(pg_session_factory):
    """Second acquirer blocks until the first transaction commits."""
    ws = uuid.uuid4()
    order: list[str] = []
    about_to_acquire = threading.Event()

    s1 = pg_session_factory()
    acquire_commit_mutex(s1, ws)
    order.append("s1-acquired")

    def second():
        with pg_session_factory() as s2:
            about_to_acquire.set()
            acquire_commit_mutex(s2, ws)  # blocks
            order.append("s2-acquired")
            s2.rollback()

    t = threading.Thread(target=second)
    t.start()
    # Wait until the worker is provably about to acquire, THEN give it time
    # to block — without the event a slow thread start would vacuously pass.
    assert about_to_acquire.wait(timeout=5)
    time.sleep(0.2)
    assert order == ["s1-acquired"]  # s2 still waiting
    s1.commit()  # xact lock released at COMMIT
    t.join(timeout=5)
    assert order == ["s1-acquired", "s2-acquired"]
    s1.close()


def test_commit_mutex_times_out_when_held(pg_session_factory):
    ws = uuid.uuid4()
    s1 = pg_session_factory()
    acquire_commit_mutex(s1, ws)
    try:
        with pg_session_factory() as s2, pytest.raises(CommitMutexTimeout):
            acquire_commit_mutex(s2, ws, timeout_seconds=1)
    finally:
        s1.rollback()
        s1.close()


def test_mutex_released_on_rollback(pg_session_factory):
    ws = uuid.uuid4()
    with pg_session_factory() as s1:
        acquire_commit_mutex(s1, ws)
        s1.rollback()
    with pg_session_factory() as s2:
        acquire_commit_mutex(s2, ws)  # must not block
        assert s2.execute(text("SELECT 1")).scalar() == 1
        s2.rollback()


def test_workspace_repo_dir_layout(tmp_path):
    ws = uuid.uuid4()
    assert workspace_repo_dir(tmp_path, ws) == tmp_path / str(ws) / "repo"


def test_current_head_missing_dir_raises_git_error(tmp_path):
    with pytest.raises(GitCommitError):
        current_head(tmp_path / "does-not-exist")


def test_current_head_does_not_walk_up_to_enclosing_repo(tmp_path):
    # tmp_path is not inside a git repo, so create one: outer repo + non-repo
    # child dir. Without GIT_CEILING_DIRECTORIES, rev-parse in the child would
    # silently report the OUTER repo's HEAD.
    outer = tmp_path / "outer"
    outer.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True, env=env)
    (outer / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=outer, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=outer, check=True, env=env)
    child = outer / "ws" / "repo"
    child.mkdir(parents=True)  # exists, NOT a git repo
    with pytest.raises(GitCommitError):
        current_head(child)
