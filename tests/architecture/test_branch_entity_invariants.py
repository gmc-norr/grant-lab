"""The two facts S2 resolves paths with. Both hold after S1; neither is obvious.

Not a unit test of the backfill (that is
tests/proposal_assist/test_branch_entity_backfill.py) — these are the properties a
later stage assumes without checking.
"""

import subprocess

from sqlalchemy import select

from call_manager.db.models import Branch, Workspace
from call_manager.proposal_assist.branch_entity_backfill import backfill_branch_entities
from call_manager.proposal_assist.branches import create_workspace_branch
from call_manager.proposal_assist.citations.gitio import head_branch, list_branches
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import (
    init_workspace_repo,
    make_user,
    make_workspace,
    stamp_for,
)


def test_every_ref_has_a_row_after_backfill_and_after_a_create(pg_session_factory, tmp_path):
    """S1's promise, over the whole set and across BOTH ways a branch appears.

    Asserted as set equality, not membership: 'new_ideas is in the rows' would
    pass with an orphan row beside it, and an orphan row is what sends a later
    path resolution to a directory that does not exist.
    """
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, "# Aims\n\nbody\n")
    subprocess.run(["git", "branch", "made_before_s1"], cwd=repo, check=True)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="made_after_s1",
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        rows = {
            n for (n,) in s.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
        }
    assert rows == set(list_branches(repo))


def test_reconciliation_leaves_an_attached_head_with_a_root_pointer(pg_session_factory, tmp_path):
    """S2 resolves `repo/` versus `worktrees/<id>/` through this pointer. A null
    one after backfill means every path for that workspace resolves nowhere.

    Two qualifiers, both load-bearing, both established by the controls below.
    ATTACHED: a detached HEAD gets its rows but keeps a null pointer, on purpose.
    AFTER RECONCILIATION: between reconciles the pointer can be null with HEAD
    attached, because `create_workspace_branch` clears it when it de-orphans the
    row the pointer named.

    Claiming the broader "a workspace with a repo is never null" would be a
    guarantee this test does not give and the reconciler does not make — S2
    resolves paths through this pointer, so an overclaim here is a null deref
    there.
    """
    with pg_session_factory() as s:
        # Distinct owners: make_workspace defaults to make_user(session) with no
        # email, and that default is the fixed literal "owner@ki.se" — a second
        # call in the same session with no explicit email collides on
        # uq_users_email.
        a = make_workspace(s, title="a", owner=make_user(s, email="a@ki.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="b@ki.se")).id
        s.commit()
    init_workspace_repo(workspace_repo_dir(tmp_path, a), "# A\n")
    init_workspace_repo(workspace_repo_dir(tmp_path, b), "# B\n")

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        for wid in (a, b):
            root_id = s.get(Workspace, wid).root_branch_id
            assert root_id is not None, f"workspace {wid} has a repo but no root pointer"
            assert s.get(Branch, root_id).workspace_id == wid


def test_the_root_pointer_never_names_another_workspaces_branch(pg_session_factory, tmp_path):
    """`root_branch_id` is a bare FK to `branches.id` with no workspace
    correlation in the constraint, so nothing in the schema stops a
    cross-workspace pointer — only the backfill's own scoping does.

    Asserted by loading each pointer and checking the branch it names BELONGS to
    the workspace holding it. `assert ra != rb` would not do: two SWAPPED pointers
    are still unequal, so that form passes on precisely the bug it names. Both
    workspaces use the same branch name, which is what a scoping bug would confuse.
    """
    with pg_session_factory() as s:
        # Distinct owners: see the note in the test above — make_workspace's
        # default owner collides on uq_users_email across two calls in one
        # session.
        a = make_workspace(s, title="a", owner=make_user(s, email="a@ki.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="b@ki.se")).id
        s.commit()
    init_workspace_repo(workspace_repo_dir(tmp_path, a), "# A\n")
    init_workspace_repo(workspace_repo_dir(tmp_path, b), "# B\n")

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        for wid in (a, b):
            root = s.get(Branch, s.get(Workspace, wid).root_branch_id)
            assert root.workspace_id == wid, f"{wid}'s root belongs to {root.workspace_id}"


def test_a_detached_head_is_the_documented_exception(pg_session_factory, tmp_path):
    """The control for the invariant above, in the same file S2 will read.

    Without it, "attached" in that test's name is an unchecked word: the
    invariant would read as universal and S2 would dereference a null pointer.
    """
    with pg_session_factory() as s:
        wid = make_workspace(s, title="d").id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, "# D\n")
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=repo, check=True)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id is None
        rows = {
            n for (n,) in s.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
        }
    # Not `set(list_branches(repo))`: on a detached HEAD git's own output carries
    # a pseudo-entry — measured here as `(HEAD detached at 893c11d)` — and
    # `_real_refs` filters it. Comparing against the raw listing would demand a row
    # for a name that is not a ref.
    assert rows == {"main"}, "the refs must still be founded"


def test_a_create_that_de_orphans_the_root_leaves_it_null_until_the_next_reconcile(
    pg_session_factory, tmp_path
):
    """The second control, and the one that contradicts the looser wording this
    file used to carry: HEAD stays ATTACHED throughout and the pointer still ends
    up null.

    Recorded here rather than only in the lifecycle tests because this is the file
    S2 reads to learn what it may assume. The second half is the actual contract:
    a reconcile restores the pointer, which is why S2 reconciling first is enough.
    """
    with pg_session_factory() as s:
        wid = make_workspace(s, title="c").id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, "# C\n")
    subprocess.run(["git", "branch", "side"], cwd=repo, check=True)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    # Leave main and destroy its ref out of band. HEAD is attached to `side`.
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
        assert head_branch(repo) == "side", "HEAD must still be attached for this to bite"
        assert s.get(Workspace, wid).root_branch_id is None

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        root = s.get(Branch, s.get(Workspace, wid).root_branch_id)
    assert root is not None and root.name == "side", "the reconcile must restore it"
