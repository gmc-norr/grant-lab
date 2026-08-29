"""The switch sequence, spec §4.2. Ordinary behaviour first, then the races and
failures — which are the criteria that decide whether this is safe. Several of
these assert by BREAKING the mechanism first; those steps are in the plan."""

import subprocess

import pytest
from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.branches import BranchBusy, BranchNotFound, switch_branch
from call_manager.proposal_assist.citations.gitio import (
    create_branch,
    current_branch,
    current_head,
)
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.room import BranchSwitchTeardown
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_workspace, stamp_for

OK = BranchSwitchTeardown(closed=0, locked=True)
NOT_LOCKED = BranchSwitchTeardown(closed=1, locked=False)


def _update_bytes(markdown: str) -> bytes:
    """A Yjs update that makes a room's document equal *markdown*, the way the
    collab tests build one (see tests/proposal_assist/collab/test_collab_room.py)."""
    from call_manager.proposal_assist.collab.canonical import markdown_to_pm_or_empty
    from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc

    return pm_json_to_ydoc(markdown_to_pm_or_empty(markdown)).get_update()


def _ws_with_repo(session_factory, tmp_path, *, text="# Aims\n\nold\n"):
    """Returns (workspace_id, repo_dir, actor_id). The actor matters — see the
    note under _update_bytes: an unauthored update never produces a commit."""
    with session_factory() as s:
        ws = make_workspace(s, title="w")
        wid, actor = ws.id, ws.owner_id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, text)
    with session_factory() as s:
        s.execute(
            Workspace.__table__.update().where(Workspace.id == wid).values(current_branch="main")
        )
        s.commit()
    return wid, repo, actor


def _rows(session_factory, wid, artifact="draft"):
    with session_factory() as s:
        return len(store.load_updates(s, workspace_id=wid, artifact=artifact))


def test_switch_moves_the_column_and_HEAD(pg_session_factory, tmp_path):
    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")

    with pg_session_factory() as s:
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="short-version",
            workspaces_dir=tmp_path,
            teardown=lambda _wid: OK,
        )
        s.commit()

    with pg_session_factory() as s:
        assert (
            s.execute(select(Workspace.current_branch).where(Workspace.id == wid)).scalar_one()
            == "short-version"
        )
    assert current_branch(repo) == "short-version"


def test_switch_checkpoints_live_edits_to_the_branch_being_LEFT(pg_session_factory, tmp_path):
    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s,
            workspace_id=wid,
            artifact="draft",
            actor_id=actor,
            update=_update_bytes("# Aims\n\nNEW LIVE TEXT\n"),
        )
        s.commit()

    with pg_session_factory() as s:
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="short-version",
            workspaces_dir=tmp_path,
            teardown=lambda _wid: OK,
        )
        s.commit()

    # The live text is on main, not on short-version.
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    assert "NEW LIVE TEXT" in (repo / "proposal.md").read_text()


def test_switch_truncates_the_log(pg_session_factory, tmp_path):
    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s,
            workspace_id=wid,
            artifact="draft",
            actor_id=actor,
            update=_update_bytes("# Aims\n\nlive\n"),
        )
        s.commit()
    assert _rows(pg_session_factory, wid) == 1

    with pg_session_factory() as s:
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="short-version",
            workspaces_dir=tmp_path,
            teardown=lambda _wid: OK,
        )
        s.commit()

    assert _rows(pg_session_factory, wid) == 0


def test_switch_bumps_the_generation(pg_session_factory, tmp_path):
    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        before = s.get(Workspace, wid).lifecycle_generation
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="short-version",
            workspaces_dir=tmp_path,
            teardown=lambda _wid: OK,
        )
        s.commit()
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).lifecycle_generation == before + 1


def test_switch_to_a_branch_that_does_not_exist_raises(pg_session_factory, tmp_path):
    wid, _repo, _actor = _ws_with_repo(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises(BranchNotFound):
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="ghost",
            workspaces_dir=tmp_path,
            teardown=lambda _wid: OK,
        )


def test_switch_to_the_CURRENT_branch_is_a_no_op(pg_session_factory, tmp_path):
    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s,
            workspace_id=wid,
            artifact="draft",
            actor_id=actor,
            update=_update_bytes("# Aims\n\nlive\n"),
        )
        s.commit()
    called = []

    with pg_session_factory() as s:
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="main",
            workspaces_dir=tmp_path,
            teardown=lambda w: called.append(w) or OK,
        )
        s.commit()

    # Nobody is evicted and no work is thrown away for a switch to where you are.
    assert called == []
    assert _rows(pg_session_factory, wid) == 1


def test_a_teardown_that_could_not_LOCK_aborts_the_switch(pg_session_factory, tmp_path):
    # Spec §4.3: locked=False means a client persist may be in flight, and that
    # persist would ride into the target branch. Refusing is free — the user retries.
    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")

    with pg_session_factory() as s, pytest.raises(BranchBusy):
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="short-version",
            workspaces_dir=tmp_path,
            teardown=lambda _wid: NOT_LOCKED,
        )
        s.commit()

    with pg_session_factory() as s:
        assert (
            s.execute(select(Workspace.current_branch).where(Workspace.id == wid)).scalar_one()
            == "main"
        )
    assert current_branch(repo) == "main"


def test_HEAD_is_reconciled_before_the_checkpoint_reads_it(pg_session_factory, tmp_path):
    # Spec §4.2 step 3. The state a crash between steps 9 and 10 leaves: HEAD on
    # the target, the column still naming the source. Without the reconcile, step
    # 5 commits the SOURCE branch's live log onto the TARGET branch.
    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    create_branch(repo, "third")
    subprocess.run(["git", "checkout", "-q", "short-version"], cwd=repo, check=True)
    # The column still says main — the crash state.
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s,
            workspace_id=wid,
            artifact="draft",
            actor_id=actor,
            update=_update_bytes("# Aims\n\nMAIN LIVE TEXT\n"),
        )
        s.commit()

    with pg_session_factory() as s:
        switch_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            target="third",
            workspaces_dir=tmp_path,
            teardown=lambda _wid: OK,
        )
        s.commit()

    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    assert "MAIN LIVE TEXT" in (repo / "proposal.md").read_text()
    subprocess.run(["git", "checkout", "-q", "short-version"], cwd=repo, check=True)
    assert "MAIN LIVE TEXT" not in (repo / "proposal.md").read_text()


def test_a_failure_after_truncate_rolls_the_DATABASE_back(
    pg_session_factory, tmp_path, monkeypatch
):
    # And leaves step 5's git commit in place, which is CORRECT: git is not in
    # the transaction, and that commit holds the source branch's own content on
    # the source branch. Spec §4.5.
    from call_manager.proposal_assist import branches as B

    wid, repo, actor = _ws_with_repo(pg_session_factory, tmp_path)
    create_branch(repo, "short-version")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s,
            workspace_id=wid,
            artifact="draft",
            actor_id=actor,
            update=_update_bytes("# Aims\n\nlive\n"),
        )
        s.commit()
    head_before = current_head(repo)

    def _boom(*a, **k):
        raise RuntimeError("checkout failed")

    monkeypatch.setattr(B, "checkout_branch", _boom)

    with pg_session_factory() as s:
        with pytest.raises(RuntimeError):
            switch_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                target="short-version",
                workspaces_dir=tmp_path,
                teardown=lambda _wid: OK,
            )
        s.rollback()

    assert _rows(pg_session_factory, wid) == 1  # log intact
    with pg_session_factory() as s:
        assert (
            s.execute(select(Workspace.current_branch).where(Workspace.id == wid)).scalar_one()
            == "main"
        )  # column intact
    assert current_head(repo) != head_before  # checkpoint commit SURVIVED
    assert current_branch(repo) == "main"  # on the source branch
