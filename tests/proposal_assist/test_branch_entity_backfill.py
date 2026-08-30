"""The branch entity: a branch is a row with an immutable id (spec §3.2).

Uses the house fixtures: `make_workspace` (tests/proposal_assist/conftest.py) and
`pg_session_factory` (tests/conftest.py). Do not construct a Workspace by hand —
it needs an owner.
"""

import subprocess
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import Branch, Workspace
from call_manager.proposal_assist.branch_entity_backfill import backfill_branch_entities
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import make_user, make_workspace


def test_a_branch_row_can_be_created_and_read_back(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        row = s.get(Branch, bid)
        assert row.workspace_id == wid
        assert row.name == "main"
        assert row.last_sdk_session_id is None
        assert row.created_at is not None


def test_one_name_per_workspace(pg_session_factory):
    """The name is a label, but it is a UNIQUE label within its workspace —
    otherwise `git branch` and the rows could disagree about how many exist."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="dup"))
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=wid, name="dup"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_same_name_in_two_workspaces_is_fine(pg_session_factory):
    """Control for the test above. Uniqueness is per workspace, not global —
    every workspace has a 'main', and a global unique key would break the
    second one silently at backfill time."""
    with pg_session_factory() as s:
        # Distinct owners: make_user's default email is the fixed literal
        # "owner@ki.se" (tests/proposal_assist/conftest.py:200-204), so two
        # make_workspace() calls with no owner= in the same session collide
        # on uq_users_email. That collision is incidental to this test's
        # subject (Branch.name uniqueness), so give each workspace its own
        # owner rather than let an unrelated fixture collision mask it.
        a = make_workspace(s, title="a", owner=make_user(s, email="branch-test-a@ki.se")).id
        b = make_workspace(s, title="b", owner=make_user(s, email="branch-test-b@ki.se")).id
        s.commit()

    with pg_session_factory() as s:
        s.add(Branch(workspace_id=a, name="main"))
        s.add(Branch(workspace_id=b, name="main"))
        s.commit()  # must not raise

    with pg_session_factory() as s:
        assert len(s.execute(select(Branch).where(Branch.name == "main")).all()) == 2


def test_root_pointer_holds_a_branch_id(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.flush()
        s.get(Workspace, wid).root_branch_id = b.id
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id == bid


def test_root_pointer_starts_null(pg_session_factory):
    """A workspace with no repo has no branch to point at. Nullable is the honest
    shape; the invariant test in Task 6 pins that a workspace WITH a repo is
    never left null."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    with pg_session_factory() as s:
        assert s.get(Workspace, wid).root_branch_id is None


def test_root_pointer_rejects_an_unknown_branch(pg_session_factory):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    with pg_session_factory() as s:
        s.get(Workspace, wid).root_branch_id = uuid.uuid4()
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_root_branch_cannot_be_deleted(pg_session_factory):
    """§7.3: 'The branch the pointer names cannot be deleted.' RESTRICT makes that
    a database guarantee rather than an application promise, so a future caller
    that forgets the check cannot destroy the root."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        b = Branch(workspace_id=wid, name="main")
        s.add(b)
        s.flush()
        s.get(Workspace, wid).root_branch_id = b.id
        s.commit()
        bid = b.id

    with pg_session_factory() as s:
        s.delete(s.get(Branch, bid))
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_non_root_branch_can_be_deleted(pg_session_factory):
    """Control. RESTRICT must protect ONLY the root — if it blocked every branch
    the delete path would be dead, and the test above would pass for the wrong
    reason."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        root = Branch(workspace_id=wid, name="main")
        other = Branch(workspace_id=wid, name="new_ideas")
        s.add_all([root, other])
        s.flush()
        s.get(Workspace, wid).root_branch_id = root.id
        s.commit()
        other_id = other.id

    with pg_session_factory() as s:
        s.delete(s.get(Branch, other_id))
        s.commit()  # must not raise
        assert s.get(Branch, other_id) is None


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def _repo(workspaces_dir, workspace_id, head, *, extra=(), commit=True, detach=False):
    """A workspace repo on branch `head`, plus one ref per name in `extra`."""
    d = workspace_repo_dir(workspaces_dir, workspace_id)
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", head, "."], cwd=d, check=True)
    if commit:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=d, check=True, env=_GIT_ENV
        )
        for name in extra:
            subprocess.run(["git", "branch", name], cwd=d, check=True)
    if detach:
        subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    return d


def _names(session, wid):
    return sorted(
        n for (n,) in session.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
    )


def test_every_local_ref_gets_a_row_not_just_the_current_one(pg_session_factory, tmp_path):
    """The whole point of step 3. `create_workspace_branch` has shipped since
    880ea243 and does nothing but `git branch`, so a workspace can already hold
    several refs. A ref with no row is invisible to every later stage."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas", "eu_version"))

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.deleted, r.rooted, r.skipped) == (3, 0, 1, 0)
        assert _names(s, wid) == ["eu_version", "main", "new_ideas"]


def test_a_slashed_branch_name_round_trips(pg_session_factory, tmp_path):
    """`git check-ref-format --branch` accepts `aims/ambitious`, and
    `test_api_branches.py:114-121` already relies on it — the delete route uses
    `{name:path}` for exactly this. §3.2 makes slashes the reason a branch needs an
    id at all, so a slashed name must survive the row it is founded into."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("aims/ambitious",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert _names(s, wid) == ["aims/ambitious", "main"]


def test_the_root_pointer_names_the_branch_repo_actually_holds(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "aims-ambitious", extra=("main",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        root_id = s.get(Workspace, wid).root_branch_id
        assert s.get(Branch, root_id).name == "aims-ambitious"


def _head_and_column_disagree(session_factory, tmp_path):
    """The state a crashed switch leaves: `repo/` is on main, the column says
    new_ideas. `_reconcile_head` (branches.py:100-126) exists because of it."""
    with session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas",))
    with session_factory() as s:
        ws = s.get(Workspace, wid)
        ws.current_branch = "new_ideas"
        ws.last_sdk_session_id = "sess-abc"
        s.commit()
    return wid


def test_the_root_pointer_follows_git_not_the_column(pg_session_factory, tmp_path):
    """§7.3 founds the root on what `repo/` actually is. Reading `current_branch`
    here would point the root at a tree nobody has."""
    wid = _head_and_column_disagree(pg_session_factory, tmp_path)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        root_id = s.get(Workspace, wid).root_branch_id
        assert s.get(Branch, root_id).name == "main", "root followed the column, not repo/"


def test_the_sdk_handle_follows_the_column_not_git(pg_session_factory, tmp_path):
    """The mirror image, on the SAME disagreeing fixture. A conversation belongs to
    the branch the USER was on, and §4.1 makes the column the source of truth for
    that (spec §8 S1 step 1). Getting these two the same way round would put the
    handle on a branch the user was never talking on."""
    wid = _head_and_column_disagree(pg_session_factory, tmp_path)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        assert {b.name: b.last_sdk_session_id for b in rows} == {
            "main": None,
            "new_ideas": "sess-abc",
        }


def test_the_sdk_handle_lands_on_one_branch_only(pg_session_factory, tmp_path):
    """§3.1: copying it to every branch would let two branches resume the same
    remote session."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.get(Workspace, wid).last_sdk_session_id = "sess-abc"
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas", "eu_version"))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        rows = s.execute(select(Branch).where(Branch.workspace_id == wid)).scalars().all()
        carried = [b.name for b in rows if b.last_sdk_session_id == "sess-abc"]
        assert carried == ["main"]


def test_the_workspace_column_is_left_alone(pg_session_factory, tmp_path):
    """Control. S4 drops it, not this stage — `turns/processor.py:389` is still the
    only reader and it still reads the workspace. Nulling it here would break
    conversation resume for every workspace with no test noticing."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.get(Workspace, wid).last_sdk_session_id = "sess-abc"
        s.commit()
    _repo(tmp_path, wid, "main")

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        assert s.get(Workspace, wid).last_sdk_session_id == "sess-abc"


def test_a_row_whose_ref_is_gone_is_deleted(pg_session_factory, tmp_path):
    """The other half of the repair. `git branch -D` is not transactional, so a
    delete whose commit failed afterwards leaves a row with no ref — and nothing
    else would ever remove it."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main")
    with pg_session_factory() as s:
        # "main" already has a row (as it would after any prior reconcile run),
        # isolating the deletion path this test is named for: without this row,
        # backfill would also CREATE "main" here, and the test would be
        # asserting created=0 against a scenario that actually creates it.
        s.add(Branch(workspace_id=wid, name="main"))
        s.add(Branch(workspace_id=wid, name="deleted_behind_our_back"))
        s.commit()

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.deleted) == (0, 1)
        assert _names(s, wid) == ["main"]


def test_an_unreadable_repo_deletes_nothing(pg_session_factory, tmp_path):
    """The dangerous direction of the reconciler. A transient git failure must
    never read as 'this workspace has no branches any more' and wipe its rows."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = _repo(tmp_path, wid, "main", extra=("new_ideas",))
    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
    (repo / ".git" / "HEAD").write_text("garbage\n")

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert r.deleted == 0
        assert r.skipped == 1
        assert _names(s, wid) == ["main", "new_ideas"]


def test_running_it_twice_changes_nothing(pg_session_factory, tmp_path):
    """A migration can be re-run against a partially-migrated database, and the
    unique key would turn that into a hard failure rather than a no-op."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas",))

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.deleted, r.rooted) == (2, 0, 1)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        # rooted is 0, not 1: the pointer was already founded, and this function
        # founds roots rather than maintaining them (§7.3 — promote moves it).
        assert (r.created, r.deleted, r.rooted) == (0, 0, 0)
        assert _names(s, wid) == ["main", "new_ideas"]


def test_a_workspace_with_no_repo_is_skipped_and_not_a_failure(pg_session_factory, tmp_path):
    """Still in setup. There is nothing to found a branch on, and inventing a
    'main' row would create a row for a ref that does not exist. It is not a
    failure either, so it must not inflate `skipped`."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted, r.skipped) == (0, 0, 0)
        assert _names(s, wid) == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_repo_with_no_commits_is_skipped(pg_session_factory, tmp_path):
    """`git init` with no commit has a branch name but no ref — there is no ref to
    found a row on."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", commit=False)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted) == (0, 0)
        assert _names(s, wid) == []


def test_a_detached_head_still_founds_the_refs_but_no_root(pg_session_factory, tmp_path):
    """Step 3 says every local ref gets a row, and detaching HEAD removes no ref —
    `main` is still there. Only the ROOT is unknown, so only the root is left
    unset. Skipping the whole workspace would violate step 3."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas",), detach=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted) == (2, 0)
        assert _names(s, wid) == ["main", "new_ideas"]
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_detached_head_founds_no_pseudo_branch(pg_session_factory, tmp_path):
    """`git branch --format=%(refname:short)` emits a pseudo-entry on a detached
    HEAD — measured, it prints `(HEAD detached at <sha>)` ABOVE the real refs.
    Founding a row from that line creates a branch every later stage would try to
    resolve to a worktree. Asserted as exact set equality: a membership check
    ('main is in there') would pass with the pseudo-row sitting beside it."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", detach=True)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert _names(s, wid) == ["main"]


def test_one_broken_repo_does_not_halt_the_rest(pg_session_factory, tmp_path):
    """A migration that dies on workspace 7 of 500 leaves the database half
    founded, and the unique key makes the retry fail differently."""
    with pg_session_factory() as s:
        # Distinct owners: make_user's default email is the fixed literal
        # "owner@ki.se" (tests/proposal_assist/conftest.py:200-204), so two
        # make_workspace() calls with no owner= in the same session collide
        # on uq_users_email, same as test_the_same_name_in_two_workspaces_is_fine
        # above.
        good_owner = make_user(s, email="broken-repo-good@ki.se")
        bad_owner = make_user(s, email="broken-repo-bad@ki.se")
        good = make_workspace(s, title="good", owner=good_owner).id
        bad = make_workspace(s, title="bad", owner=bad_owner).id
        s.commit()
    _repo(tmp_path, good, "main")
    broken = workspace_repo_dir(tmp_path, bad)
    broken.mkdir(parents=True)
    (broken / ".git").write_text("not a git dir")

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted, r.skipped) == (1, 1, 1)
        assert _names(s, good) == ["main"]
        assert _names(s, bad) == []


def test_reconciling_survives_the_root_branchs_ref_being_deleted(pg_session_factory, tmp_path):
    """`fk_workspaces_root_branch_id` is ON DELETE RESTRICT, so deleting a row the
    workspace still points at raises ForeignKeyViolation — and this function holds
    ONE transaction for every workspace, so that would abort the whole batch and
    discard every workspace already reconciled.

    Not a contrived state: today's delete route guards only `current_branch`
    (`branches.py:348`), so switching off `main` and deleting it is allowed.

    The second workspace is the point. It is reconciled BEFORE the broken one in
    the same call, so if the delete raises, its rows roll back with everything
    else — asserting only on the broken workspace would miss the collateral.
    """
    with pg_session_factory() as s:
        broken = make_workspace(s, title="broken", owner=make_user(s, email="br@ki.se")).id
        other = make_workspace(s, title="other", owner=make_user(s, email="ot@ki.se")).id
        s.commit()
    for wid in (broken, other):
        _repo(tmp_path, wid, "main", extra=("new_ideas",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    d = workspace_repo_dir(tmp_path, broken)
    subprocess.run(["git", "checkout", "-q", "new_ideas"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=d, check=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)  # must not raise
        s.commit()
        assert r.deleted == 1
        assert _names(s, broken) == ["new_ideas"]
        assert _names(s, other) == ["main", "new_ideas"], "the healthy workspace rolled back"
        # repo/ is on new_ideas now, so that is the root the reconciler resolves.
        assert s.get(Branch, s.get(Workspace, broken).root_branch_id).name == "new_ideas"


def test_a_rootless_workspace_whose_root_ref_vanished_is_left_honestly_rootless(
    pg_session_factory, tmp_path
):
    """The other branch of the same fix: no new root is knowable AND the old root's
    ref is gone. Clearing the pointer is the honest answer — pointing it at a
    surviving branch would silently promote something nobody chose, and §7.3 gives
    that power to promote alone."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="rootless@ki.se")).id
        s.commit()
    _repo(tmp_path, wid, "main", extra=("new_ideas",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    d = workspace_repo_dir(tmp_path, wid)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=d, check=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)  # must not raise
        s.commit()
        assert (r.deleted, r.rooted) == (1, 0)
        assert _names(s, wid) == ["new_ideas"]
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_surviving_root_pointer_is_not_cleared_by_an_unrelated_orphan(
    pg_session_factory, tmp_path
):
    """Control for the test above. The clear must fire ONLY when the doomed row is
    the one the pointer names — a blanket 'detached plus orphans, so null it'
    would silently unroot every healthy workspace that happened to have a stale
    row lying around."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="survive@ki.se")).id
        s.commit()
    _repo(tmp_path, wid, "main")

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.add(Branch(workspace_id=wid, name="stale_unrelated"))
        s.commit()
        root_before = s.get(Workspace, wid).root_branch_id

    d = workspace_repo_dir(tmp_path, wid)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert r.deleted == 1
        assert s.get(Workspace, wid).root_branch_id == root_before, "unrooted a healthy workspace"


def test_the_migration_calls_the_backfill_inside_upgrade():
    """The migration body cannot be executed in a unit test — it needs a live
    Alembic op context — so pin the wiring by PARSING it.

    A substring search would not do: `assert "backfill_branch_entities" in source`
    is satisfied by the import line alone, by a comment, or by dead code after a
    `return`. That is the exact shape of a gate that passes because its subject is
    already present. This walks the AST of `upgrade()` and requires an actual Call
    whose callee is named `backfill_branch_entities`.
    """
    import ast
    import pathlib

    src = pathlib.Path("alembic/versions/0068_branch_entity.py").read_text()
    tree = ast.parse(src)
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    called = {
        n.func.id
        for n in ast.walk(upgrade)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "backfill_branch_entities" in called, sorted(called)


def test_reconciling_after_a_switch_does_not_move_the_root(pg_session_factory, tmp_path):
    """§7.3 gives moving the root to promote alone. `switch_branch` correctly
    leaves the pointer be — so a reconciler that re-derived it from git HEAD would
    hand that power to whoever switched last.

    Measured before the guard existed: switch to `new_ideas`, re-run, and the
    pointer moved to `new_ideas`, reporting `created=0, deleted=0, rooted=1`. The
    workspace's real root then became deletable, and `git branch -D` is not
    recoverable through the UI.

    The delete at the end is the point. Asserting only that the pointer did not
    move would pass against a fix that moved it somewhere else harmless; what
    actually matters is that the root stays undeletable.
    """
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w", owner=make_user(s, email="noswitch@ki.se")).id
        s.commit()
    repo = _repo(tmp_path, wid, "main", extra=("new_ideas",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        founded = s.get(Branch, s.get(Workspace, wid).root_branch_id).name
    assert founded == "main"

    subprocess.run(["git", "checkout", "-q", "new_ideas"], cwd=repo, check=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert r.rooted == 0, "a re-run re-founded a root that was already set"
        assert s.get(Branch, s.get(Workspace, wid).root_branch_id).name == "main", (
            "the reconciler moved the root to whatever repo/ was checked out on"
        )


def test_a_workspace_with_no_repo_is_counted_absent_not_silently_zero(pg_session_factory, tmp_path):
    """`WORKSPACES_DIR` unset, resolved against the wrong cwd, or an unmounted
    volume makes EVERY workspace look like one still in setup — and the run then
    prints the same clean zero an empty instance prints. Counting them is what
    makes those two distinguishable in the migration's output."""
    with pg_session_factory() as s:
        a = make_workspace(s, title="a", owner=make_user(s, email="abs-a@ki.se")).id
        make_workspace(s, title="b", owner=make_user(s, email="abs-b@ki.se"))
        s.commit()
    _repo(tmp_path, a, "main")  # one real repo, one workspace with none

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert (r.created, r.rooted, r.skipped, r.absent) == (1, 1, 0, 1)
