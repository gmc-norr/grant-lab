"""Create and delete keep the rows and the refs in agreement.

A one-time backfill goes stale the moment the next branch is created, and the app
keeps serving POST /branches between this stage and S2 (spec §8 S1 step 4).
"""

import subprocess

import pytest
from sqlalchemy import select
from sqlalchemy import update as sql_update

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


def test_create_over_an_orphaned_ROOT_row_still_returns_a_branch(pg_session_factory, tmp_path):
    """The sibling of the test above, for the one orphan the FK will not let go.

    `fk_workspaces_root_branch_id` is ON DELETE RESTRICT, so when the orphan being
    replaced is the row `root_branch_id` names, the de-orphan delete raises
    ForeignKeyViolation. Reproduced 2026-08-31 with the ref ALREADY created — a
    201 turned into a 500 AND left git changed, which is the exact failure the
    de-orphan step exists to prevent, one state further in.

    Reachable without a bug: `git branch -D` run outside the app, or a restore
    whose database snapshot is newer than the repo volume.
    """
    wid, repo = _ws(pg_session_factory, tmp_path)
    subprocess.run(["git", "branch", "side"], cwd=repo, check=True)
    # Leave main and destroy its ref. The root row now names a ref that is gone.
    subprocess.run(["git", "checkout", "-q", "side"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=repo, check=True)
    with pg_session_factory() as s:
        s.get(Workspace, wid).current_branch = "side"
        s.commit()

    with pg_session_factory() as s:
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="main", workspaces_dir=tmp_path
        )
        s.commit()

    with pg_session_factory() as s:
        # Only `main`: `side` was made with raw git in this test and never had a
        # row. Founding it is the reconciler's job, not create's.
        assert _rows(s, wid) == ["main"]
        # The pointer is CLEARED, not moved to the replacement: `git branch main`
        # does not move HEAD, so the new ref is not known to be the root.
        # `backfill_branch_entities` re-roots it once HEAD agrees.
        assert s.get(Workspace, wid).root_branch_id is None
    assert branch_exists(repo, "main")


def test_the_orphaned_root_is_cleared_before_git_runs(pg_session_factory, tmp_path):
    """Ordering, not just outcome. If the pointer clear ran AFTER
    `git_create_branch`, a failure in the row work would leave a ref behind that
    the caller's rollback cannot undo — git is not transactional and this
    function's caller commits later.

    Probed by reading the pointer FROM INSIDE the git call. The obvious test —
    make git raise and check nothing was half-done — is vacuous: after the
    rollback both orderings look identical, and it passed with the fix reverted.
    Measured 2026-08-31, which is why this one reads the intermediate state
    instead of the final one.
    """
    wid, repo = _ws(pg_session_factory, tmp_path)
    subprocess.run(["git", "branch", "side"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "side"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=repo, check=True)
    with pg_session_factory() as s:
        s.get(Workspace, wid).current_branch = "side"
        s.commit()

    import call_manager.proposal_assist.branches as branches_mod

    real = branches_mod.git_create_branch
    seen = {}

    with pg_session_factory() as s:

        def _spy(r, n):
            # Same session, so this sees the flushed-but-uncommitted UPDATE.
            s.expire_all()
            seen["root_when_git_ran"] = s.get(Workspace, wid).root_branch_id
            return real(r, n)

        branches_mod.git_create_branch = _spy
        try:
            create_workspace_branch(
                s, stamp=stamp_for(s, wid), workspace_id=wid, name="main", workspaces_dir=tmp_path
            )
            s.commit()
        finally:
            branches_mod.git_create_branch = real

    assert seen["root_when_git_ran"] is None, (
        "git ran while the workspace still pointed at the orphan — "
        "the de-orphan step is after the git call again"
    )
    assert branch_exists(repo, "main")


def test_a_root_pointer_founded_during_the_mutex_wait_still_refuses(pg_session_factory, tmp_path):
    """The guard must judge the CURRENT row, not the one the route already loaded.

    `require_owner` (web/_perms.py:18-24) loads the Workspace into the session's
    identity map before the route calls us, so a plain `session.get` returns that
    instance with no SQL. `acquire_commit_mutex_at_generation` is
    `pg_advisory_xact_lock`: no row lock, no ORM refresh — so a writer committing
    during a wait of up to 10s is invisible to the guard.

    Reproduced before the fix: the root ref was DESTROYED, and only then did
    RESTRICT raise — a rollback git cannot undo.
    """
    wid, repo = _ws(pg_session_factory, tmp_path)
    subprocess.run(["git", "branch", "new_ideas"], cwd=repo, check=True)
    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="new_ideas"))
        w = s.get(Workspace, wid)
        w.current_branch = "new_ideas"
        w.root_branch_id = None  # vacant, as create-over-an-orphaned-root leaves it
        s.commit()

    with pg_session_factory() as route:
        # Exactly what require_owner does: this caches the instance.
        loaded = route.scalar(select(Workspace).where(Workspace.id == wid))
        assert loaded.root_branch_id is None

        with pg_session_factory() as other:  # another request, commits underneath
            root = other.execute(
                select(Branch).where(Branch.workspace_id == wid, Branch.name == "main")
            ).scalar_one()
            other.execute(
                sql_update(Workspace).where(Workspace.id == wid).values(root_branch_id=root.id)
            )
            other.commit()

        with pytest.raises(BranchInUse):
            delete_workspace_branch(
                route,
                stamp=stamp_for(route, wid),
                workspace_id=wid,
                name="main",
                workspaces_dir=tmp_path,
            )
        route.rollback()

    assert branch_exists(repo, "main"), "the root ref was destroyed before the refusal"


def test_the_row_delete_runs_before_git_so_RESTRICT_protects_the_ref(pg_session_factory, tmp_path):
    """The backstop, proved by ORDER rather than by outcome.

    The obvious test — set the pointer at the branch and expect the FK to refuse —
    is hollow: the readable guard above catches that case first, so git never runs
    for the guard's reason and the assertion passes without the ordering mattering
    at all. Caught 2026-08-31 while writing it.

    So probe the intermediate state instead: delete an ordinary branch that
    succeeds, and ask FROM INSIDE the git call whether the row is already gone.
    If it is, the FK has already had its chance while git was still untouched.
    """
    wid, repo = _ws(pg_session_factory, tmp_path)
    subprocess.run(["git", "branch", "new_ideas"], cwd=repo, check=True)
    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="new_ideas"))
        s.commit()

    import call_manager.proposal_assist.branches as branches_mod

    real = branches_mod.git_delete_branch
    seen = {}

    with pg_session_factory() as s:

        def _spy(r, n):
            seen["rows_when_git_ran"] = _rows(s, wid)
            return real(r, n)

        branches_mod.git_delete_branch = _spy
        try:
            delete_workspace_branch(
                s,
                stamp=stamp_for(s, wid),
                workspace_id=wid,
                name="new_ideas",
                workspaces_dir=tmp_path,
            )
            s.commit()
        finally:
            branches_mod.git_delete_branch = real

    assert seen["rows_when_git_ran"] == ["main"], (
        "the row was still present when git ran — the FK cannot protect a ref "
        f"that git has already destroyed (saw {seen['rows_when_git_ran']})"
    )
    assert not branch_exists(repo, "new_ideas")
