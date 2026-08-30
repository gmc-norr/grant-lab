"""Create and delete keep the rows and the refs in agreement.

A one-time backfill goes stale the moment the next branch is created, and the app
keeps serving POST /branches between this stage and S2 (spec §8 S1 step 4).
"""

import subprocess

import pytest
from sqlalchemy import select

from call_manager.db.models import Branch, Workspace
from call_manager.proposal_assist.branches import (
    BranchExists,
    BranchInUse,
    create_workspace_branch,
    delete_workspace_branch,
)
from call_manager.proposal_assist.citations.gitio import branch_exists, list_branches
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_workspace, stamp_for


def _ws(session_factory, tmp_path):
    with session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, "# Aims\n\nbody\n")
    with session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.flush()
        s.get(Workspace, wid).root_branch_id = b.id
        s.commit()
    return wid, repo


def _rows(session, wid):
    return sorted(
        n for (n,) in session.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
    )


def test_create_writes_a_row_beside_the_ref(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    assert branch_exists(repo, "new_ideas")
    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main", "new_ideas"]


def test_the_new_row_carries_a_fresh_id_and_no_sdk_handle(pg_session_factory, tmp_path):
    """§3.2: the id is the identity. A new branch has held no conversation, so its
    resume handle starts empty rather than inheriting the parent's — sharing one
    would interleave two branches' turns upstream (§3.1)."""
    wid, _ = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        s.execute(
            select(Branch).where(Branch.workspace_id == wid, Branch.name == "main")
        ).scalar_one().last_sdk_session_id = "sess-parent"
        s.commit()

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        by_name = {b.name: b for b in rows}
        assert by_name["new_ideas"].last_sdk_session_id is None
        assert by_name["new_ideas"].id != by_name["main"].id


def test_a_refused_create_writes_no_row(pg_session_factory, tmp_path):
    """The ref and the row must fail together. A row for a branch git refused to
    create is exactly the divergence this task exists to prevent."""
    wid, _ = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(BranchExists):
            create_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name="new_ideas",
                workspaces_dir=tmp_path,
            )
        # Assert BEFORE the rollback, on the session's PENDING set. Asserting on
        # committed rows after a rollback proves nothing: the rollback discards a
        # wrongly-queued row wherever in the function it was added, so the
        # committed state looks identical either way. Measured — moving
        # `session.add` above the refusal check reddens no test without this.
        pending = [o for o in s.new if isinstance(o, Branch)]
        assert pending == [], f"a refused create queued {[b.name for b in pending]}"
        s.rollback()

    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main", "new_ideas"]


def test_rows_and_refs_agree_after_a_create(pg_session_factory, tmp_path):
    """The invariant stated as itself, over the whole set. A subset assertion
    ('new_ideas is in there') would pass while an orphan row sat beside it."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        for name in ("new_ideas", "eu_version"):
            create_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name=name,
                workspaces_dir=tmp_path,
            )
        s.commit()

    with pg_session_factory() as s:
        assert _rows(s, wid) == sorted(list_branches(repo))


def test_delete_removes_the_row_with_the_ref(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        delete_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()

    assert not branch_exists(repo, "new_ideas")
    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main"]


def test_delete_refuses_the_root_branch(pg_session_factory, tmp_path):
    """§7.3: the branch the root pointer names cannot be deleted. Raised as
    BranchInUse — the error a user can read — with the database RESTRICT as the
    backstop behind it."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.get(Workspace, wid).current_branch = "new_ideas"  # so the OLD guard cannot fire
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(BranchInUse):
            delete_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name="main",
                workspaces_dir=tmp_path,
            )
        s.rollback()

    assert branch_exists(repo, "main"), "the ref was destroyed before the refusal"
    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main", "new_ideas"]


def test_rows_and_refs_agree_after_a_delete(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        for name in ("new_ideas", "eu_version"):
            create_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name=name,
                workspaces_dir=tmp_path,
            )
        s.commit()

    with pg_session_factory() as s:
        delete_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="eu_version",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        assert _rows(s, wid) == sorted(list_branches(repo))


def test_deleting_a_branch_with_no_row_still_drops_the_ref(pg_session_factory, tmp_path):
    """A ref created before this stage — or by `git branch` outside the app — has
    no row. Delete must not raise on the missing row and strand the ref forever."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    subprocess.run(["git", "branch", "orphan"], cwd=repo, check=True)

    with pg_session_factory() as s:
        delete_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="orphan",
            workspaces_dir=tmp_path,
        )
        s.commit()

    assert not branch_exists(repo, "orphan")
    with pg_session_factory() as s:
        assert _rows(s, wid) == ["main"]


def test_create_over_an_orphaned_row_still_returns_a_branch(pg_session_factory, tmp_path):
    """An orphan row — a delete whose commit failed after `git branch -D`, or a
    restore whose database snapshot is newer than the repo volume — used to make
    the NEXT create of that name blow up on `uq_branches_ws_name`. `_STATUS` has
    no mapping for IntegrityError, so a create that returned 201 before this
    branch returned a 500, with git having already made the ref.

    `branch_exists` has just answered NO, so any row of that name is by
    definition stale and is cleared first.
    """
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="new_ideas"))  # row, no ref
        s.commit()

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="new_ideas",
            workspaces_dir=tmp_path,
        )
        s.commit()  # must not raise IntegrityError

    assert branch_exists(repo, "new_ideas")
    with pg_session_factory() as s:
        assert _rows(s, wid) == sorted(list_branches(repo))
