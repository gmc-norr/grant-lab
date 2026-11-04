"""Migration 0068: the branch entity, founded from each workspace's real refs.

Moved from tests/proposal_assist/test_branch_entity_backfill.py on 2026-11-03,
when the proposal workspace left master. The reconciler those tests exercised is
now a frozen copy inside alembic/versions/0068_branch_entity.py, and these tests
call it there. The last test runs upgrade() itself on rows that predate it.

Left behind in the move, with the reason:
  * the Branch schema tests (unique name per workspace, RESTRICT on the root):
    they test table constraints, not the backfill, and the table is parked;
  * the `workspace_id=` narrowing test: only Phase 0 passed it, and the frozen
    copy does not take it;
  * the two AST tests of upgrade()'s wiring: `test_upgrade_0068_*` below runs
    upgrade() for real, which shows the call is reachable and comes after the
    schema it writes;
  * the AST test that no whole-entity SELECT appears: the frozen copy has no ORM
    entities (test_frozen_migrations_import_no_app_code.py pins that it imports
    no application code at all);
  * the root-guard half of the switch test: it called the removed
    `delete_workspace_branch`.
Added: the cross-workspace root check from the deleted
tests/architecture/test_branch_entity_invariants.py, the one property in that
file that belongs to the backfill alone.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, select, text

from alembic import command
from grant_lab.db.models import Branch, Workspace
from tests.migrations.support import (
    alembic_cfg,
    git_repo,
    load_migration,
    make_user,
    make_workspace,
    repo_dir,
)

M = load_migration("0068_branch_entity.py")


def _run(session_factory, root):
    """One reconcile in its own session, committed. Returns the counts."""
    with session_factory() as s:
        r = M._backfill_branch_entities(s.connection(), workspaces_dir=root)
        s.commit()
    return r


def _names(session, wid):
    return sorted(
        n for (n,) in session.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
    )


def _root_name(session, wid):
    root_id = session.get(Workspace, wid).root_branch_id
    return None if root_id is None else session.get(Branch, root_id).name


def test_every_local_ref_gets_a_row_not_just_the_current_one(pg_session_factory, tmp_path):
    """`create_workspace_branch` did nothing but `git branch`, so a workspace
    could already hold several refs. A ref with no row is invisible."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas", "eu_version"))

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.deleted, r.rooted, r.skipped) == (3, 0, 1, 0)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["eu_version", "main", "new_ideas"]


def test_a_slashed_branch_name_round_trips(pg_session_factory, tmp_path):
    """`git check-ref-format --branch` accepts `aims/ambitious`; a slashed name
    must survive the row it is founded into."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("aims/ambitious",))

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["aims/ambitious", "main"]


def test_the_root_pointer_names_the_branch_repo_actually_holds(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "aims-ambitious", extra=("main",))

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _root_name(s, wid) == "aims-ambitious"


def test_the_root_pointer_never_names_another_workspaces_branch(pg_session_factory, tmp_path):
    """`root_branch_id` is a bare FK to `branches.id`: nothing in the schema stops
    a cross-workspace pointer, only the backfill's own scoping. Both workspaces
    use the same branch name, which is what a scoping bug would confuse; checked
    by ownership, because two SWAPPED pointers are still unequal."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="a", owner=make_user(s, email="a@umu.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="b@umu.se")).id
        s.commit()
    git_repo(tmp_path, a, "main")
    git_repo(tmp_path, b, "main")

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        for wid in (a, b):
            root = s.get(Branch, s.get(Workspace, wid).root_branch_id)
            assert root.workspace_id == wid, f"{wid}'s root belongs to {root.workspace_id}"


def _head_and_column_disagree(session_factory, tmp_path):
    """The state a crashed switch left: `repo/` is on main, the column says
    new_ideas."""
    with session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    with session_factory() as s:
        ws = s.get(Workspace, wid)
        ws.current_branch = "new_ideas"
        ws.last_sdk_session_id = "sess-abc"
        s.commit()
    return wid


def test_the_root_pointer_follows_git_not_the_column(pg_session_factory, tmp_path):
    """The root is founded on what `repo/` actually is."""
    wid = _head_and_column_disagree(pg_session_factory, tmp_path)
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _root_name(s, wid) == "main", "root followed the column, not repo/"


def test_the_sdk_handle_follows_the_column_not_git(pg_session_factory, tmp_path):
    """The mirror image, on the SAME fixture: a conversation belongs to the branch
    the USER was on, and the column is the source of truth for that."""
    wid = _head_and_column_disagree(pg_session_factory, tmp_path)
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        assert {b.name: b.last_sdk_session_id for b in rows} == {
            "main": None,
            "new_ideas": "sess-abc",
        }


def test_the_sdk_handle_lands_on_one_branch_only(pg_session_factory, tmp_path):
    """Copying it to every branch would let two branches resume one session."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.get(Workspace, wid).last_sdk_session_id = "sess-abc"
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas", "eu_version"))

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        assert [b.name for b in rows if b.last_sdk_session_id == "sess-abc"] == ["main"]


def test_the_workspace_column_is_left_alone(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.get(Workspace, wid).last_sdk_session_id = "sess-abc"
        s.commit()
    git_repo(tmp_path, wid, "main")

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).last_sdk_session_id == "sess-abc"


def test_a_row_whose_ref_is_gone_is_deleted(pg_session_factory, tmp_path):
    """`git branch -D` is not transactional, so a delete whose commit failed
    afterwards leaves a row with no ref, and nothing else would remove it."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main")
    with pg_session_factory() as s:
        # "main" already has a row, so this run deletes and does not also create.
        s.add(Branch(workspace_id=wid, name="main"))
        s.add(Branch(workspace_id=wid, name="deleted_behind_our_back"))
        s.commit()

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.deleted) == (0, 1)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main"]


def test_an_unreadable_repo_deletes_nothing(pg_session_factory, tmp_path):
    """A git failure must never read as 'this workspace has no branches'."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    _run(pg_session_factory, tmp_path)
    (repo / ".git" / "HEAD").write_text("garbage\n")

    r = _run(pg_session_factory, tmp_path)
    assert (r.deleted, r.skipped) == (0, 1)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main", "new_ideas"]


def test_running_it_twice_changes_nothing(pg_session_factory, tmp_path):
    """A migration can be re-run against a partially-migrated database, and the
    unique key would turn a non-idempotent pass into a hard failure."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas",))

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.deleted, r.rooted) == (2, 0, 1)
    r = _run(pg_session_factory, tmp_path)
    # rooted is 0, not 1: the pointer was already founded, and founding is all
    # this does.
    assert (r.created, r.deleted, r.rooted) == (0, 0, 0)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main", "new_ideas"]


def test_a_workspace_with_no_repo_is_absent_not_skipped(pg_session_factory, tmp_path):
    """Still in setup: nothing to found a branch on, and not a failure."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.rooted, r.skipped, r.absent) == (0, 0, 0, 1)
    with pg_session_factory() as s:
        assert _names(s, wid) == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_workspace_with_no_repo_is_counted_absent_not_silently_zero(pg_session_factory, tmp_path):
    """An unset WORKSPACES_DIR makes EVERY workspace look like one in setup.
    Counting them is what tells that apart from an empty instance."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="a", owner=make_user(s, email="abs-a@umu.se")).id
        make_workspace(s, title="b", owner=make_user(s, email="abs-b@umu.se"))
        s.commit()
    git_repo(tmp_path, a, "main")  # one real repo, one workspace with none

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.rooted, r.skipped, r.absent) == (1, 1, 0, 1)


def test_a_repo_with_no_commits_is_a_no_op(pg_session_factory, tmp_path):
    """`git init` with no commit has a branch name but no ref to found a row on."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", commit=False)

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.deleted, r.rooted, r.skipped, r.absent) == (0, 0, 0, 0, 0), r
    with pg_session_factory() as s:
        assert _names(s, wid) == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_detached_head_still_founds_the_refs_but_no_root(pg_session_factory, tmp_path):
    """Detaching HEAD removes no ref, so every ref still gets a row. Only the ROOT
    is unknown, so only the root is left unset."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas",), detach=True)

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.rooted) == (2, 0)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main", "new_ideas"]
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_detached_head_founds_no_pseudo_branch(pg_session_factory, tmp_path):
    """On a detached HEAD `git branch` prints `(HEAD detached at <sha>)` above the
    real refs. Asserted as exact equality: a membership check would pass with the
    pseudo-row beside `main`."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main", detach=True)

    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["main"]


def test_one_broken_repo_does_not_halt_the_rest(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        good = make_workspace(s, title="good", owner=make_user(s, email="good@umu.se")).id
        bad = make_workspace(s, title="bad", owner=make_user(s, email="bad@umu.se")).id
        s.commit()
    git_repo(tmp_path, good, "main")
    broken = repo_dir(tmp_path, bad)
    broken.mkdir(parents=True)
    (broken / ".git").write_text("not a git dir")

    r = _run(pg_session_factory, tmp_path)
    assert (r.created, r.rooted, r.skipped) == (1, 1, 1)
    with pg_session_factory() as s:
        assert _names(s, good) == ["main"]
        assert _names(s, bad) == []


def test_reconciling_survives_the_root_branchs_ref_being_deleted(pg_session_factory, tmp_path):
    """fk_workspaces_root_branch_id is ON DELETE RESTRICT and one transaction
    covers every workspace, so deleting the row the pointer names would abort the
    batch. The second workspace gets a NEW ref in the second run, so a rolled-back
    batch would show as its missing row."""
    with pg_session_factory() as s:
        broken = make_workspace(s, title="broken", owner=make_user(s, email="br@umu.se")).id
        other = make_workspace(s, title="other", owner=make_user(s, email="ot@umu.se")).id
        s.commit()
    for wid in (broken, other):
        git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    _run(pg_session_factory, tmp_path)

    d = repo_dir(tmp_path, broken)
    subprocess.run(["git", "checkout", "-q", "new_ideas"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=d, check=True)
    subprocess.run(["git", "branch", "third"], cwd=repo_dir(tmp_path, other), check=True)

    r = _run(pg_session_factory, tmp_path)  # must not raise
    assert r.deleted == 1
    with pg_session_factory() as s:
        assert _names(s, broken) == ["new_ideas"]
        assert _names(s, other) == ["main", "new_ideas", "third"], "the batch rolled back"
        assert _root_name(s, broken) == "new_ideas"


def test_a_rootless_workspace_whose_root_ref_vanished_is_left_honestly_rootless(
    pg_session_factory, tmp_path
):
    """No new root is knowable (detached) AND the old root's ref is gone: clear the
    pointer rather than promote a survivor nobody chose."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="rootless@umu.se")).id
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    _run(pg_session_factory, tmp_path)

    d = repo_dir(tmp_path, wid)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=d, check=True)

    r = _run(pg_session_factory, tmp_path)  # must not raise
    assert (r.deleted, r.rooted) == (1, 0)
    with pg_session_factory() as s:
        assert _names(s, wid) == ["new_ideas"]
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_surviving_root_pointer_is_not_cleared_by_an_unrelated_orphan(
    pg_session_factory, tmp_path
):
    """Control for the test above: the clear fires ONLY when the doomed row is the
    one the pointer names."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="survive@umu.se")).id
        s.commit()
    git_repo(tmp_path, wid, "main")
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="stale_unrelated"))
        s.commit()
        root_before = s.get(Workspace, wid).root_branch_id

    subprocess.run(
        ["git", "checkout", "-q", "--detach", "HEAD"], cwd=repo_dir(tmp_path, wid), check=True
    )
    r = _run(pg_session_factory, tmp_path)
    assert r.deleted == 1
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id == root_before, "unrooted a healthy workspace"


def test_reconciling_after_a_switch_does_not_move_the_root(pg_session_factory, tmp_path):
    """The root is FOUNDED, never moved: a re-run after a switch must leave it.
    (The original also proved the root stayed undeletable, through the removed
    `delete_workspace_branch`; that half went with it.)"""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="noswitch@umu.se")).id
        s.commit()
    repo = git_repo(tmp_path, wid, "main", extra=("new_ideas",))
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert _root_name(s, wid) == "main"

    subprocess.run(["git", "checkout", "-q", "new_ideas"], cwd=repo, check=True)
    r = _run(pg_session_factory, tmp_path)
    assert r.rooted == 0, "a re-run re-founded a root that was already set"
    with pg_session_factory() as s:
        assert _root_name(s, wid) == "main", "the root moved to what repo/ had checked out"


def test_the_sdk_handle_lands_on_exactly_one_branch_after_a_switch(pg_session_factory, tmp_path):
    """Reconcile, switch (which moves current_branch and leaves the workspace's
    handle), reconcile: the handle must end on ONE row. Asserted over the whole
    map, because `held["feature"] == "sess-X"` passes with main still holding it."""
    with pg_session_factory() as s:
        w = make_workspace(s, title="w")
        wid = w.id
        w.current_branch = "main"
        w.last_sdk_session_id = "sess-X"
        s.commit()
    git_repo(tmp_path, wid, "main", extra=("feature",))
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:  # what switch_branch left behind
        s.get(Workspace, wid).current_branch = "feature"
        s.commit()
    _run(pg_session_factory, tmp_path)

    with pg_session_factory() as s:
        held = dict(
            s.execute(
                select(Branch.name, Branch.last_sdk_session_id).where(Branch.workspace_id == wid)
            ).all()
        )
    assert held == {"feature": "sess-X", "main": None}, held


def test_current_branch_is_not_nullable_so_the_null_path_is_unreachable(migrated_engine):
    """Why no test sets current_branch to NULL: the column is NOT NULL, so the
    `is_distinct_from` (rather than `!=`) in the clearing UPDATE guards a path no
    row can reach. Pinned on the database, not the ORM."""
    cols = {c["name"]: c for c in inspect(migrated_engine).get_columns("workspaces")}
    assert cols["current_branch"]["nullable"] is False


def test_a_repo_whose_last_ref_vanished_is_swept(pg_session_factory, tmp_path):
    """A readable repo with ZERO refs still needs the sweep: its stale row and
    pointer would otherwise stay forever."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    d = git_repo(tmp_path, wid, "main")
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id is not None

    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=d, check=True)
    r = _run(pg_session_factory, tmp_path)
    assert r.deleted == 1, r
    with pg_session_factory() as s:
        assert _names(s, wid) == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_an_unreadable_HEAD_skips_the_workspace_instead_of_clearing_its_root(
    pg_session_factory, tmp_path, monkeypatch
):
    """An operational git failure must not be read as "detached HEAD", which is
    the branch that CLEARS the root pointer. Set up on the destructive path (the
    root's ref is gone), so a run that did not skip WOULD clear it."""
    with pg_session_factory() as s:
        w = make_workspace(s, title="w")
        wid = w.id
        w.current_branch = "main"
        s.commit()
    d = git_repo(tmp_path, wid, "main", extra=("side",))
    _run(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        root_before = s.get(Workspace, wid).root_branch_id
    assert root_before is not None

    subprocess.run(["git", "checkout", "-q", "side"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=d, check=True)
    subprocess.run(["git", "branch", "third"], cwd=d, check=True)

    def _unreadable(_repo_dir):
        raise M.GitCommitError("cannot read HEAD")

    monkeypatch.setattr(M, "_head_branch", _unreadable)
    r = _run(pg_session_factory, tmp_path)
    assert (r.skipped, r.created, r.deleted, r.rooted) == (1, 0, 0, 0), r
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id == root_before
        assert _names(s, wid) == ["main", "side"]


def test_an_unreadable_directory_skips_one_workspace_not_the_batch(pg_session_factory, tmp_path):
    """`Path.exists()` RAISES on EACCES. Unreadable counts as `skipped`, not
    `absent`, and the healthy workspace is still founded."""
    with pg_session_factory() as s:
        bad = make_workspace(s, title="bad", owner=make_user(s, email="bad@umu.se")).id
        good = make_workspace(s, title="good", owner=make_user(s, email="good@umu.se")).id
        s.commit()
    git_repo(tmp_path, good, "main")
    locked = repo_dir(tmp_path, bad)
    locked.mkdir(parents=True)
    (locked / ".git").mkdir()
    os.chmod(locked, 0o000)
    try:
        r = _run(pg_session_factory, tmp_path)
    finally:
        os.chmod(locked, 0o755)

    assert (r.skipped, r.created) == (1, 1), r
    with pg_session_factory() as s:
        assert s.get(Workspace, good).root_branch_id is not None


def test_founding_the_root_does_not_restamp_updated_at(pg_session_factory, tmp_path):
    """The original named `updated_at` with its own value to suppress the model's
    onupdate, because a restamp reordered every user's workspace list. Asserted on
    the exact timestamp."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    git_repo(tmp_path, wid, "main")
    with pg_session_factory() as s:
        before = s.get(Workspace, wid).updated_at

    r = _run(pg_session_factory, tmp_path)
    assert r.rooted == 1, r
    with pg_session_factory() as s:
        ws = s.get(Workspace, wid)
        assert ws.root_branch_id is not None
        assert ws.updated_at == before, "the founding restamped updated_at"


@pytest.mark.parametrize(
    "boom",
    [
        pytest.param(OSError("disk gone"), id="OSError"),
        pytest.param(subprocess.TimeoutExpired(cmd=["git"], timeout=30.0), id="TimeoutExpired"),
    ],
)
def test_every_arm_of_the_skip_handler_is_reachable(
    pg_session_factory, tmp_path, monkeypatch, boom
):
    """`subprocess.TimeoutExpired` is NOT an OSError, so dropping it from the
    handler would let a 30 s git timeout abort the whole migration."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="arm@umu.se")).id
        s.commit()
    git_repo(tmp_path, wid, "main")

    def _raise(_repo_dir):
        raise boom

    monkeypatch.setattr(M, "_list_branches", _raise)
    r = _run(pg_session_factory, tmp_path)
    assert (r.skipped, r.created, r.rooted) == (1, 0, 0), r


def test_timeout_is_not_an_OSError_which_is_why_both_are_listed():
    assert not issubclass(subprocess.TimeoutExpired, OSError)


_SUMMARY = re.compile(
    r"0068: founded (\d+) branch rows, removed (\d+) orphans, set (\d+) root pointers, "
    r"SKIPPED (\d+) unreadable repos, (\d+) workspaces with no repo directory"
)


def test_upgrade_0068_founds_branches_for_rows_that_predate_it(
    pg_url, pg_session_factory, tmp_path, monkeypatch, capsys
):
    """upgrade() itself: downgrade to 0067, seed workspaces and their repos there,
    upgrade to 0068 with WORKSPACES_DIR pointing at the repos, read the rows back.

    This is what proves upgrade() calls the frozen copy, with the env var, after
    `branches` and `root_branch_id` exist. `pg_session_factory` is requested only
    for its teardown TRUNCATE.
    """
    cfg = alembic_cfg(pg_url)
    eng = create_engine(pg_url)
    stamp = datetime(2020, 1, 1, tzinfo=UTC)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0067")
        try:
            with eng.begin() as c:
                uid = c.execute(
                    text("INSERT INTO users (email) VALUES ('m0068@umu.se') RETURNING id")
                ).scalar_one()

                def ws(title, current_branch="main", sdk=None):
                    return c.execute(
                        text(
                            "INSERT INTO workspaces (id, title, owner_id, current_branch, "
                            "last_sdk_session_id, updated_at) VALUES (gen_random_uuid(), "
                            ":t, :u, :cb, :sdk, :at) RETURNING id"
                        ),
                        {"t": title, "u": uid, "cb": current_branch, "sdk": sdk, "at": stamp},
                    ).scalar_one()

                several = ws("several", current_branch="new_ideas", sdk="sess-0068")
                detached = ws("detached")
                no_repo = ws("no repo")
                broken = ws("broken")
            git_repo(tmp_path, several, "main", extra=("new_ideas", "eu_version"))
            git_repo(tmp_path, detached, "main", extra=("side",), detach=True)
            bad = repo_dir(tmp_path, broken)
            bad.mkdir(parents=True)
            (bad / ".git").write_text("not a git dir")
            monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
            capsys.readouterr()  # drop anything printed before the upgrade

            command.upgrade(cfg, "0068")

            out = capsys.readouterr().out
            with eng.connect() as c:
                branches = c.execute(
                    text(
                        "SELECT b.workspace_id, b.name, b.last_sdk_session_id FROM branches b "
                        "JOIN workspaces w ON w.id = b.workspace_id WHERE w.owner_id = :u"
                    ),
                    {"u": uid},
                ).all()
                roots = c.execute(
                    text(
                        "SELECT w.id, b.name, w.updated_at FROM workspaces w "
                        "LEFT JOIN branches b ON b.id = w.root_branch_id WHERE w.owner_id = :u"
                    ),
                    {"u": uid},
                ).all()
        finally:
            command.upgrade(cfg, "head")
    finally:
        eng.dispose()

    by_ws: dict = {}
    for wid, name, sdk in branches:
        by_ws.setdefault(wid, {})[name] = sdk
    assert by_ws == {
        several: {"main": None, "new_ideas": "sess-0068", "eu_version": None},
        detached: {"main": None, "side": None},
    }
    assert {wid: root for wid, root, _ in roots} == {
        several: "main",
        detached: None,
        no_repo: None,
        broken: None,
    }
    assert {at for _, _, at in roots} == {stamp}, "the founding restamped updated_at"
    m = _SUMMARY.search(out)
    assert m, f"0068 printed no summary: {out!r}"
    founded, _removed, rooted, skipped, absent = map(int, m.groups())
    # At least, not exactly: the database is shared by the whole suite, and a
    # workspace another test left behind would be counted too.
    assert founded >= 5 and rooted >= 1 and skipped >= 1 and absent >= 1, out
