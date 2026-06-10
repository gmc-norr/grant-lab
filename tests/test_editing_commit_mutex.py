import threading
import time
import uuid

import pytest
from sqlalchemy import text

from call_manager.proposal_assist.citations.gitio import GitCommitError, current_head
from call_manager.proposal_assist.drafting.locking import (
    acquire_commit_mutex,
    commit_mutex_key,
    workspace_lock_key,
)
from call_manager.proposal_assist.paths import workspace_repo_dir


def test_commit_mutex_key_differs_from_subprocess_lock_key():
    ws = uuid.uuid4()
    assert commit_mutex_key(ws) != workspace_lock_key(ws)


def test_commit_mutex_serializes_two_sessions(pg_session_factory):
    """Second acquirer blocks until the first transaction commits."""
    ws = uuid.uuid4()
    order: list[str] = []

    s1 = pg_session_factory()
    acquire_commit_mutex(s1, ws)
    order.append("s1-acquired")

    def second():
        with pg_session_factory() as s2:
            acquire_commit_mutex(s2, ws)  # blocks
            order.append("s2-acquired")
            s2.rollback()

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.3)
    assert order == ["s1-acquired"]  # s2 still waiting
    s1.commit()  # xact lock released at COMMIT
    t.join(timeout=5)
    assert order == ["s1-acquired", "s2-acquired"]
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
