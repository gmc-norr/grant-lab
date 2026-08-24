"""acquire_commit_mutex_live: the live-workspace guard on the commit mutex.

The point of the helper is the FRESH read. session.get() can return the
identity-map instance loaded before we blocked on the mutex — which is exactly
the instance a writer holds while a deletion commits underneath it.
"""

import threading
import uuid as _uuid
from datetime import UTC, datetime

import pytest

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    WorkspaceDeleted,
    acquire_commit_mutex,
    acquire_commit_mutex_live,
)
from tests.proposal_assist.conftest import make_workspace


def test_live_workspace_acquires_normally(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        acquire_commit_mutex_live(s, ws_id)  # must not raise
        s.rollback()


def test_deleted_workspace_raises_before_any_mutation(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted) as e:
            acquire_commit_mutex_live(s, ws_id)
        assert e.value.workspace_id == ws_id
        s.rollback()


def test_missing_workspace_raises(pg_session_factory):
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            acquire_commit_mutex_live(s, _uuid.uuid4())
        s.rollback()


def test_reads_fresh_state_not_the_identity_map(pg_session_factory):
    """REGRESSION (spec review P1-3): the writer loaded the Workspace BEFORE
    blocking on the mutex. A session.get() would return that stale instance and
    the guard would pass on a workspace that is already deleted."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as writer:
        stale = writer.get(Workspace, ws_id)  # warms the identity map
        assert stale.soft_deleted_at is None

        with pg_session_factory() as deleter:  # committed by ANOTHER session
            deleter.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            deleter.commit()

        with pytest.raises(WorkspaceDeleted):
            acquire_commit_mutex_live(writer, ws_id)
        writer.rollback()


def test_writer_blocked_on_the_mutex_sees_a_deletion_that_commits_meanwhile(
    pg_session_factory,
):
    """The linearization point. The writer blocks on the mutex; the deleter holds
    it, marks the workspace deleted, and commits. The writer must wake up and
    refuse."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    holder_has_mutex = threading.Event()
    writer_is_blocking = threading.Event()
    result: dict = {}

    def _deleter():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holder_has_mutex.set()
            writer_is_blocking.wait(timeout=5)  # let the writer queue behind us
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()  # releases the mutex

    def _writer():
        holder_has_mutex.wait(timeout=5)
        with pg_session_factory() as s:
            writer_is_blocking.set()
            try:
                acquire_commit_mutex_live(s, ws_id)
                result["outcome"] = "acquired"
            except WorkspaceDeleted:
                result["outcome"] = "refused"
            except CommitMutexTimeout:
                result["outcome"] = "timeout"
            s.rollback()

    threads = [threading.Thread(target=_deleter), threading.Thread(target=_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert result["outcome"] == "refused"


def test_app_registers_a_workspace_deleted_handler(_app):
    """The single mapping every migrated writer relies on: no per-route except
    blocks, so if this registration is lost the refusals become 500s."""
    assert WorkspaceDeleted in _app.exception_handlers


def test_save_section_refuses_a_deleted_workspace(pg_session_factory, tmp_path):
    """Representative service-level writer: raises before any git commit."""
    import subprocess

    from call_manager.proposal_assist.editing.save import PROPOSAL_FILENAME, save_section
    from call_manager.proposal_assist.ports.stamp import stamp_for_workspace

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(md0)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with pytest.raises(WorkspaceDeleted):
            save_section(
                session=s,
                workspace=ws,
                stamp=stamp_for_workspace(ws),
                user_id=ws.owner_id,
                section_index=1,
                new_text="## Background\nedited.\n",
                base_commit_sha=head,
                base_section_sha="0" * 64,
                workspaces_dir=str(tmp_path),
            )
        s.rollback()

    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert after == head  # no commit


def test_submit_user_turn_keeps_its_own_stronger_guard(pg_session_factory):
    """PER-SITE DECISION: submit already re-checks under a FOR UPDATE row lock and
    raises ValueError (mapped to 400). Pinned so a later 'uniformity' refactor
    doesn't silently change that route's status code."""
    from call_manager.config import Settings
    from call_manager.proposal_assist.turns.submit import submit_user_turn

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id, owner_id = ws.id, ws.owner_id
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(ValueError):  # NOT WorkspaceDeleted
            submit_user_turn(
                session=s,
                workspace_id=ws_id,
                content_text="hi",
                submitted_by_user_id=owner_id,
                settings=Settings(),
            )
        s.rollback()
