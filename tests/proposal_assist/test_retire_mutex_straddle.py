"""Spec test 12 — real writers BLOCKED on the commit mutex when a deletion
commits must wake up, refuse, and mutate nothing.

Testing the helper in isolation proves nothing about the actual entry points.
Shape, for every case:

  1. a holder thread takes the commit mutex and parks;
  2. the writer thread calls the REAL entry point -> queues behind it;
  3. we poll pg_locks until the writer is genuinely blocked (a sleep would race
     on a loaded machine);
  4. the holder marks the workspace deleted and COMMITS, releasing the mutex;
  5. the writer wins the mutex, re-reads, and must refuse having written no git
     commit and no DB row.

Step 4 uses a SEPARATE session that holds the mutex and stamps soft_deleted_at
directly — NOT soft_delete_workspace, which would try to take the mutex this
same thread already owns. Do not "simplify" that into a deadlock.
"""

import subprocess
import threading
import time
import uuid as uuidlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from call_manager.db.models import CollabArtifactState, ProposedChange, User, Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.kernel import kernel_accept_prose
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.drafting.locking import (
    WorkspaceDeleted,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.ports.stamp import stamp_for_workspace
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for


def _advisory_waiters(session_factory) -> int:
    """Count ungranted advisory locks. The test DB is otherwise idle, so a
    non-zero count means our writer really is queued behind the mutex — not
    merely 'started'."""
    with session_factory() as s:
        return s.scalar(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        )


def _wait_until_blocked(session_factory, *, n: int = 1, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _advisory_waiters(session_factory) >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"no writer blocked on the advisory lock within {timeout}s")


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _straddle(pg_session_factory, ws_id, writer):
    """Run `writer` (a no-arg callable) so that it blocks on the commit mutex, then
    commit a deletion underneath it. Returns whatever the writer thread recorded."""
    holding = threading.Event()
    out: dict = {}

    def _writer_thread():
        holding.wait(timeout=10)
        writer(out)

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            # Wait until the writer is genuinely QUEUED behind us, then delete.
            _wait_until_blocked(pg_session_factory, n=1, timeout=10)
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()  # releases the mutex; the writer wakes up next

    threads = [threading.Thread(target=_holder), threading.Thread(target=_writer_thread)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return out


def test_blocked_save_section_refuses_and_leaves_git_untouched(pg_session_factory, tmp_path):
    """The legacy git-first writer."""
    from call_manager.proposal_assist.editing.save import save_section

    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="straddle-save@ki.se"))
        s.commit()
        ws_id = ws.id
    repo = Path(tmp_path) / str(ws_id) / "repo"
    _init_repo(repo, md0)
    head = _git_head(repo)
    base_sha = split_sections(md0)[1].sha

    def _writer(out):
        with pg_session_factory() as s:
            wsr = s.get(Workspace, ws_id)  # loaded BEFORE we block — the stale instance
            try:
                save_section(
                    session=s,
                    workspace=wsr,
                    stamp=stamp_for_workspace(wsr),
                    user_id=wsr.owner_id,
                    section_index=1,
                    new_text="## Background\nedited by a blocked writer.\n",
                    base_commit_sha=head,
                    base_section_sha=base_sha,
                    workspaces_dir=str(tmp_path),
                )
                out["r"] = "committed"
            except WorkspaceDeleted:
                out["r"] = "refused"
            finally:
                s.rollback()

    out = _straddle(pg_session_factory, ws_id, _writer)

    assert out["r"] == "refused"
    assert _git_head(repo) == head  # no commit


def test_blocked_kernel_accept_refuses_and_leaves_the_proposal_pending(
    pg_session_factory, tmp_path
):
    """The collab writer — and the case that fails if the kernel acquire is
    swapped in place rather than hoisted above the early-exit mutation."""
    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="straddle-kernel@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        s.commit()
        ws_id, oid = ws.id, owner.id
    repo = Path(tmp_path) / str(ws_id) / "repo"
    _init_repo(repo, md0)
    head = _git_head(repo)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        stamp = stamp_for(s, ws_id)  # the writer's belief, formed while live
        updates = load_or_hydrate(
            s, stamp=stamp, workspace_id=ws_id, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
        room_doc = build_doc(updates)
    live_md = pm_to_markdown(ydoc_to_pm_json(room_doc))

    # STALE on purpose: this proposal takes the early-exit branch that resolves the
    # row ABOVE the original acquire. Under a straddle, an un-hoisted acquire
    # durably flips it to "stale" on a deleted workspace.
    with pg_session_factory() as s:
        pc = ProposedChange(
            id=uuidlib.uuid4(),
            workspace_id=ws_id,
            status="pending",
            target_artifact="draft",
            section_index=1,
            base_commit_sha=head,
            base_section_sha="0" * 64,
            kind="prose_edit",
            payload={"new_text": "## Background\nnever applied.\n"},
            origin_kind="human",
        )
        s.add(pc)
        s.commit()
        pc_id = pc.id
    assert split_sections(live_md)[1].sha != "0" * 64  # genuinely stale

    def _writer(out):
        with pg_session_factory() as s:
            try:
                kernel_accept_prose(
                    s,
                    stamp=stamp,
                    room_doc=room_doc,
                    proposed_change_id=pc_id,
                    workspace_id=ws_id,
                    approver=s.get(User, oid),
                    workspaces_dir=str(tmp_path),
                )
                s.commit()
                out["r"] = "resolved"
            except WorkspaceDeleted:
                out["r"] = "refused"
            finally:
                s.rollback()

    out = _straddle(pg_session_factory, ws_id, _writer)

    assert out["r"] == "refused"
    assert _git_head(repo) == head
    with pg_session_factory() as s:
        row = s.get(ProposedChange, pc_id)
        assert row.status == "pending"  # NOT "stale"
        assert row.resolved_by is None
        assert row.resolved_at is None


def test_blocked_enable_on_touch_returns_false_and_writes_no_state_row(
    pg_session_factory, tmp_path
):
    """The flip.py variant: it COMMITS a CollabArtifactState row, so it must not
    run on a workspace whose deletion landed while it waited."""
    from call_manager.proposal_assist.collab.flip import maybe_auto_enable

    md0 = "## Background\noriginal.\n"  # a live workspace here WOULD be enabled
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="straddle-flip@ki.se"))
        s.commit()
        ws_id = ws.id
    repo = Path(tmp_path) / str(ws_id) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        stamp = stamp_for(s, ws_id)  # the read route's belief, formed while live

    def _writer(out):
        with pg_session_factory() as s:
            out["r"] = maybe_auto_enable(
                s,
                stamp=stamp,
                workspace_id=ws_id,
                artifact="draft",
            )
            s.rollback()

    out = _straddle(pg_session_factory, ws_id, _writer)

    assert out["r"] is False
    with pg_session_factory() as s:
        rows = (
            s.execute(
                select(CollabArtifactState).where(CollabArtifactState.workspace_id == ws_id)
            )
            .scalars()
            .all()
        )
        assert [r for r in rows if getattr(r, "enabled", False)] == []


def test_the_blocking_probe_actually_observes_a_lock_wait(pg_session_factory):
    """Guards the guard: if _wait_until_blocked returned immediately, every test
    above would be synchronizing on nothing and could pass by luck."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="probe@ki.se"))
        s.commit()
        ws_id = ws.id

    # Nothing contending: the probe must TIME OUT rather than report a waiter.
    with pytest.raises(AssertionError):
        _wait_until_blocked(pg_session_factory, n=1, timeout=0.5)

    # With a real waiter queued behind a holder, it must observe it.
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _waiter():
        holding.wait(timeout=10)
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id, timeout_seconds=20)
            s.rollback()

    threads = [threading.Thread(target=_holder), threading.Thread(target=_waiter)]
    for t in threads:
        t.start()
    try:
        holding.wait(timeout=10)
        _wait_until_blocked(pg_session_factory, n=1, timeout=10)  # must NOT raise
    finally:
        release.set()
        for t in threads:
            t.join(timeout=30)
