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
from call_manager.proposal_assist.citations.gitio import GitCommitError
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

    Not a contrived state — though no longer via the delete route, which this
    same branch gave a root guard. The reachable producers are `git branch -D` run
    outside the app, and a restore whose database snapshot is newer than the repo
    volume.

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


def _live_statements(body):
    """The statements in *body* that can actually run, flattened, in order.

    `ast.walk` is the wrong tool for a reachability question: it yields every
    node in the subtree, so a call parked after an unconditional `return`, or
    inside `if False:`, is still "found". Both are one-line mutations, and both
    left the gate below green when it used `ast.walk`. Measured 2026-08-31:
    inserting a bare `return` above the call, the test passed in 0.07s.

    So: stop at the first `return`/`raise` at each level, and drop the branch of
    an `if` whose test is a literal that decides it.
    """
    import ast

    out = []
    for stmt in body:
        out.append(stmt)
        if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Constant):
            out.extend(_live_statements(stmt.body if stmt.test.value else stmt.orelse))
        else:
            for field in ("body", "orelse", "finalbody", "handlers"):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list):
                    out.extend(_live_statements(inner))
        if isinstance(stmt, ast.Return | ast.Raise):
            break  # nothing after this at this level ever runs
    return out


def _own_nodes(stmt):
    """*stmt* and its expression descendants, NOT descending into nested statements.

    Load-bearing: a compound statement (`if`, `with`, `try`) carries its whole
    body in its subtree, so a plain `ast.walk` over it would find a call inside a
    dead branch that `_live_statements` had already excluded. Every live nested
    statement appears in that list on its own, so nothing reachable is lost.
    """
    import ast

    yield stmt
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, ast.stmt):
            continue
        yield from _own_nodes(child)


def _call_index(statements, callee):
    """Index of the first live statement containing a call to *callee*, or None."""
    import ast

    for i, stmt in enumerate(statements):
        for node in _own_nodes(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if fname == callee:
                    return i
    return None


def test_the_migration_calls_the_backfill_inside_upgrade():
    """The migration body cannot be executed in a unit test — it needs a live
    Alembic op context — so pin the wiring by PARSING it.

    A substring search would not do: `assert "backfill_branch_entities" in source`
    is satisfied by the import line alone, or by a comment. Nor would an
    `ast.walk` over the whole function: that yields dead code too, and a call the
    migration can never reach founds nothing. So this asks the two questions that
    actually matter — is the call REACHABLE, and does it run AFTER the table it
    populates exists.
    """
    import ast
    import pathlib

    src = pathlib.Path("alembic/versions/0068_branch_entity.py").read_text()
    tree = ast.parse(src)
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    live = _live_statements(upgrade.body)

    backfill_at = _call_index(live, "backfill_branch_entities")
    assert backfill_at is not None, "the backfill call in upgrade() is unreachable or absent"

    create_at = _call_index(live, "create_table")
    assert create_at is not None, "upgrade() no longer creates the table"
    assert create_at < backfill_at, (
        f"the backfill runs at statement {backfill_at}, before create_table at {create_at}"
    )


def test_the_migration_gate_rejects_a_call_it_cannot_reach():
    """The control for the test above — without this, `_live_statements` could
    return everything and nobody would notice.

    Two shapes, because they are the two one-line mutations that a reviewer
    reaches for first, and the `ast.walk` version this replaced survived both.
    """
    import ast

    after_return = ast.parse("def upgrade():\n    return\n    backfill_branch_entities(s)\n")
    in_dead_branch = ast.parse(
        "def upgrade():\n    if False:\n        backfill_branch_entities(s)\n"
    )
    for tree in (after_return, in_dead_branch):
        live = _live_statements(tree.body[0].body)
        assert _call_index(live, "backfill_branch_entities") is None, ast.dump(tree)

    reachable = ast.parse("def upgrade():\n    backfill_branch_entities(s)\n")
    assert _call_index(_live_statements(reachable.body[0].body), "backfill_branch_entities") == 0


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


def test_the_sdk_handle_lands_on_exactly_one_branch_after_a_switch(pg_session_factory, tmp_path):
    """§3.1: one hosted conversation, so at most one branch may name it.

    The trap is that `switch_branch` moves `current_branch` and deliberately does
    NOT clear `workspaces.last_sdk_session_id`. So reconcile, switch, reconcile
    used to leave the SAME id on both rows — measured
    `{'feature': 'sess-X', 'main': 'sess-X'}` — and two branches would resume one
    remote session. The docstring named that harm; the code did not prevent it.

    Reachable the moment S2 lands: this function's own contract says "S2 runs it
    again before it attaches any worktree".
    """
    with pg_session_factory() as s:
        w = make_workspace(s, title="w")
        wid = w.id
        w.current_branch = "main"
        w.last_sdk_session_id = "sess-X"
        s.commit()
    _repo(tmp_path, wid, "main", extra=("feature",))

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:  # what switch_branch leaves behind
        s.get(Workspace, wid).current_branch = "feature"
        s.commit()

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()

    with pg_session_factory() as s:
        held = {
            n: sid
            for n, sid in s.execute(
                select(Branch.name, Branch.last_sdk_session_id).where(Branch.workspace_id == wid)
            ).all()
        }
    # Asserted over the WHOLE map, not `held["feature"] == "sess-X"`: that form
    # passes with main still holding it, which is the entire bug.
    assert held == {"feature": "sess-X", "main": None}, held


def test_current_branch_is_not_nullable_so_the_null_path_is_unreachable():
    """Why there is no NULL-`current_branch` test above.

    `is_distinct_from` rather than `!=` in the clearing UPDATE is deliberate —
    `name != NULL` is NULL in SQL, so a `!=` predicate would match no rows and
    leave every stale handle in place. But the column is NOT NULL today, so that
    path cannot be reached, and a test that sets it to NULL fails on the
    constraint rather than exercising anything (measured: NotNullViolation).

    Pinned as a fact instead. If S4 ever relaxes this column, this test goes red
    and the NULL case becomes reachable and worth writing.
    """
    assert Workspace.__table__.c.current_branch.nullable is False


def test_a_repo_whose_last_ref_vanished_is_swept(pg_session_factory, tmp_path):
    """A readable repo with ZERO refs still needs the sweep.

    It used to `continue` before the rows were even loaded, so a repo whose final
    ref was deleted under a detached HEAD kept its stale row and its stale root
    pointer forever — nothing else revisits an established workspace. Measured
    before the fix: `BackfillResult(0, 0, 0, 0, 0)`, row `['main']` still present,
    pointer still set.
    """
    with pg_session_factory() as s:
        w = make_workspace(s, title="w")
        wid = w.id
        w.current_branch = "main"
        s.commit()
    _repo(tmp_path, wid, "main")
    d = workspace_repo_dir(tmp_path, wid)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        assert s.get(Workspace, wid).root_branch_id is not None

    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=d, check=True)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
    assert r.deleted == 1, r

    with pg_session_factory() as s:
        rows = [
            n for (n,) in s.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
        ]
        assert rows == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_a_fresh_repo_with_no_commit_is_still_a_no_op(pg_session_factory, tmp_path):
    """The control for the test above. Removing the early `continue` must not turn
    `git init` with no commit — which also has zero refs — into a founding or a
    delete. Same input shape, opposite expectation."""
    with pg_session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    _repo(tmp_path, wid, "main", commit=False)

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
    assert (r.created, r.deleted, r.rooted, r.skipped, r.absent) == (0, 0, 0, 0, 0), r

    with pg_session_factory() as s:
        assert s.execute(select(Branch).where(Branch.workspace_id == wid)).all() == []
        assert s.get(Workspace, wid).root_branch_id is None


def test_an_unreadable_HEAD_skips_the_workspace_instead_of_clearing_its_root(
    pg_session_factory, tmp_path, monkeypatch
):
    """An operational git failure must not be read as "detached HEAD".

    `current_branch` raises the same GitCommitError for a detached HEAD and for a
    broken repo, so catching it made a timeout look detached — and detached is
    the branch that CLEARS the root pointer. A transient failure could therefore
    destroy healthy state and still report success, because nothing counted it.

    Set up on the destructive path: the root's ref is gone, so a successful run
    WOULD clear the pointer (`test_a_rootless_workspace_whose_root_ref_vanished_
    is_left_honestly_rootless` pins that). Here git cannot read HEAD, so the run
    must change nothing at all and say so.
    """
    with pg_session_factory() as s:
        w = make_workspace(s, title="w")
        wid = w.id
        w.current_branch = "main"
        s.commit()
    _repo(tmp_path, wid, "main", extra=("side",))
    d = workspace_repo_dir(tmp_path, wid)

    with pg_session_factory() as s:
        backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
        root_before = s.get(Workspace, wid).root_branch_id
    assert root_before is not None

    subprocess.run(["git", "checkout", "-q", "side"], cwd=d, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=d, check=True)
    subprocess.run(["git", "branch", "third"], cwd=d, check=True)

    def _unreadable(_repo_dir):
        raise GitCommitError("cannot read HEAD")

    monkeypatch.setattr(
        "call_manager.proposal_assist.branch_entity_backfill.head_branch", _unreadable
    )

    with pg_session_factory() as s:
        r = backfill_branch_entities(s, workspaces_dir=tmp_path)
        s.commit()
    assert (r.skipped, r.created, r.deleted, r.rooted) == (1, 0, 0, 0), r

    with pg_session_factory() as s:
        # The pointer survives, the orphan survives, and the NEW ref got no row —
        # the whole workspace was skipped, which is what `skipped` now means.
        assert s.get(Workspace, wid).root_branch_id == root_before
        rows = {
            n for (n,) in s.execute(select(Branch.name).where(Branch.workspace_id == wid)).all()
        }
    assert rows == {"main", "side"}, rows


def test_an_unreadable_directory_skips_one_workspace_not_the_batch(pg_session_factory, tmp_path):
    """`Path.exists()` RAISES on EACCES — measured 2026-08-31, it does not return
    False. The probe sat outside the try, so ONE unreadable directory aborted the
    whole batch; in migration 0068 that is a failed deploy.

    Unreadable counts as `skipped`, not `absent`: the repo may well be there.
    """
    import os

    with pg_session_factory() as s:
        bad = make_workspace(s, title="bad", owner=make_user(s, email="bad@ki.se")).id
        good = make_workspace(s, title="good", owner=make_user(s, email="good@ki.se")).id
        s.commit()
    _repo(tmp_path, good, "main")

    locked = workspace_repo_dir(tmp_path, bad)
    locked.mkdir(parents=True)
    (locked / ".git").mkdir()
    os.chmod(locked, 0o000)
    try:
        with pg_session_factory() as s:
            r = backfill_branch_entities(s, workspaces_dir=tmp_path)
            s.commit()
    finally:
        os.chmod(locked, 0o755)

    assert r.skipped == 1, r
    assert r.created == 1, f"the healthy workspace was not founded: {r}"
    with pg_session_factory() as s:
        assert s.get(Workspace, good).root_branch_id is not None


def test_the_reconciler_never_selects_a_whole_workspace_row():
    """Migration 0068 runs this function, so its SQL must name only the columns
    the schema had AT 0068. A `session.get(Workspace, ...)` emits a SELECT over
    every column models.py maps TODAY — so the next column added to `workspaces`
    would break the upgrade path for every database below 0068.

    Pinned by parsing rather than by mocking: an entity load is a source-level
    property, and a mock that returned the right value would hide it.
    """
    import ast
    import pathlib

    src = pathlib.Path("src/call_manager/proposal_assist/branch_entity_backfill.py").read_text()
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # session.get(Workspace, ...) / session.get(Branch, ...)
        if isinstance(f, ast.Attribute) and f.attr == "get" and node.args:
            a0 = node.args[0]
            if isinstance(a0, ast.Name) and a0.id in {"Workspace", "Branch"}:
                offenders.append(f"session.get({a0.id}, ...) at line {node.lineno}")
        # select(Workspace) / select(Branch) — the entity, not a column
        if isinstance(f, ast.Name) and f.id == "select":
            for a in node.args:
                if isinstance(a, ast.Name) and a.id in {"Workspace", "Branch"}:
                    offenders.append(f"select({a.id}) at line {node.lineno}")
    assert offenders == [], offenders
