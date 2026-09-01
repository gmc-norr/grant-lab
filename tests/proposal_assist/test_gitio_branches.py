"""git branch primitives. Every expectation here was measured against real git
on 2026-08-29 before being written down — including the one that is
counter-intuitive (an unborn HEAD still has a branch NAME)."""

import subprocess

import pytest

from call_manager.proposal_assist.citations import gitio
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    branch_exists,
    branch_tips,
    checkout_branch,
    commit_exists,
    create_branch,
    current_branch,
    delete_branch,
    has_commits,
    head_branch,
    is_valid_branch_name,
    list_branches,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def _repo(tmp_path, *, commit=True):
    d = tmp_path / "repo"
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=d, check=True)
    if commit:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "first"],
            cwd=d,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "PATH": "/usr/bin:/bin",
            },
        )
    return d


def test_current_branch_reads_the_checked_out_branch(tmp_path):
    assert current_branch(_repo(tmp_path)) == "main"


def test_current_branch_works_on_an_UNBORN_head(tmp_path):
    # Measured: `git symbolic-ref --short HEAD` exits 0 and prints "main" on a
    # repo with no commits. The migration depends on this — a workspace still in
    # setup must not fail the backfill.
    assert current_branch(_repo(tmp_path, commit=False)) == "main"


def test_current_branch_raises_on_a_DETACHED_head(tmp_path):
    d = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    with pytest.raises(GitCommitError):
        current_branch(d)


def test_create_then_list_and_exists(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "aims-ambitious")
    assert sorted(list_branches(d)) == ["aims-ambitious", "main"]
    assert branch_exists(d, "aims-ambitious") is True
    assert branch_exists(d, "nope") is False
    # Creating must NOT move HEAD.
    assert current_branch(d) == "main"


def test_create_on_an_unborn_repo_raises(tmp_path):
    # Measured: `git branch x HEAD` exits 128 with
    # "fatal: not a valid object name: 'HEAD'".
    with pytest.raises(GitCommitError):
        create_branch(_repo(tmp_path, commit=False), "x")


def test_has_commits_distinguishes_unborn_from_born(tmp_path):
    # current_branch CANNOT make this distinction — it succeeds on both.
    assert has_commits(_repo(tmp_path)) is True
    assert has_commits(_repo(tmp_path / "b", commit=False)) is False


def test_checkout_moves_head(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "short-version")
    checkout_branch(d, "short-version")
    assert current_branch(d) == "short-version"


def test_read_branch_file_keeps_the_resolved_commit_when_the_branch_moves(
    tmp_path, monkeypatch
):
    d = _repo(tmp_path)
    proposal = d / "proposal.md"
    proposal.write_text("old\n")
    subprocess.run(["git", "add", "proposal.md"], cwd=d, check=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-qm", "old"], cwd=d, check=True, env=_GIT_ENV)
    old_head = gitio.current_head(d)
    real_read = gitio.read_file_at_commit

    def move_branch_then_read(repo_dir, commit_sha, path):
        proposal.write_text("new\n")
        subprocess.run(
            ["git", "commit", "-qam", "new"], cwd=d, check=True, env=_GIT_ENV
        )
        return real_read(repo_dir, commit_sha, path)

    monkeypatch.setattr(gitio, "read_file_at_commit", move_branch_then_read)

    head, text = gitio.read_branch_file(d, "main", "proposal.md")

    assert gitio.current_head(d) != old_head
    assert head == old_head
    assert text == "old\n"


def test_delete_removes_the_ref(tmp_path):
    d = _repo(tmp_path)
    create_branch(d, "dead-end")
    delete_branch(d, "dead-end")
    assert branch_exists(d, "dead-end") is False


def test_delete_is_force_and_removes_an_UNMERGED_branch(tmp_path):
    # -D not -d: the branch a user most wants to throw away is the one whose
    # commits went nowhere, and `-d` refuses exactly that.
    d = _repo(tmp_path)
    create_branch(d, "abandoned")
    checkout_branch(d, "abandoned")
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "work"],
        cwd=d,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        },
    )
    checkout_branch(d, "main")
    delete_branch(d, "abandoned")
    assert branch_exists(d, "abandoned") is False


def test_delete_of_the_current_branch_raises(tmp_path):
    # git refuses this itself (exit 1, "cannot delete branch ... used by
    # worktree"). The app refuses first, but this proves the floor exists.
    d = _repo(tmp_path)
    with pytest.raises(GitCommitError):
        delete_branch(d, "main")


@pytest.mark.parametrize("name", ["a..b", "foo.lock", "-lead", "with space", ""])
def test_invalid_names_are_rejected(name):
    # Delegated to `git check-ref-format --branch`. Measured 2026-08-29: git
    # rejects each of these. Do NOT hand-roll the rules.
    assert is_valid_branch_name(name) is False


@pytest.mark.parametrize("name", ["good-name", "aims/ambitious", "v2"])
def test_valid_names_are_accepted(name):
    assert is_valid_branch_name(name) is True


def test_probes_raise_on_a_FATAL_git_failure_rather_than_answering_no(tmp_path):
    """Measured 2026-08-29: `show-ref`/`rev-parse --verify --quiet` exit 1 for a
    normal "no" and 128 when the directory is not a repository. Collapsing 128 to
    False would surface repo corruption as "branch not found" / "no commits yet"."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(GitCommitError):
        branch_exists(not_a_repo, "main")
    with pytest.raises(GitCommitError):
        has_commits(not_a_repo)


def test_head_branch_returns_the_name_when_head_is_ATTACHED(tmp_path):
    assert head_branch(_repo(tmp_path)) == "main"


def test_head_branch_returns_None_on_a_DETACHED_head(tmp_path):
    """The whole point of this function. `current_branch` RAISES here, and it
    raises the same GitCommitError it raises for a broken repository — so a
    caller that catches it cannot tell "no branch" from "no git"."""
    d = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD"], cwd=d, check=True)
    assert head_branch(d) is None
    with pytest.raises(GitCommitError):
        current_branch(d)  # the contrast that makes head_branch necessary


def test_head_branch_works_on_an_UNBORN_head(tmp_path):
    """Same surprise as `current_branch`: git knows the name the first commit
    will land on, and exit 0 means that name is real."""
    assert head_branch(_repo(tmp_path, commit=False)) == "main"


def test_head_branch_RAISES_on_a_fatal_failure_rather_than_saying_detached(tmp_path):
    """The discrimination, asserted where it can actually go wrong.

    Measured 2026-08-31: `symbolic-ref --short -q HEAD` exits **1** on a detached
    HEAD and **128** when it is not a repository — and without `-q` a detached
    HEAD exits 128 too, which is exactly why the lenient form cannot be used.
    Returning None here would tell the reconciler "detached", and the reconciler
    CLEARS a root pointer on detached.
    """
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()  # exists, so git actually runs and exits 128
    with pytest.raises(GitCommitError):
        head_branch(not_a_repo)


def test_git_reads_do_not_ESCAPE_a_broken_repo_into_the_enclosing_one(tmp_path):
    """git discovery walks UPWARD until it finds any repository.

    On this deployment `var/workspaces/` sits inside the application's own
    checkout, so a workspace repo whose `.git` is corrupt used to answer with the
    SERVER'S branches — exit 0, no error. The reconciler would then found rows
    named after the server's branches and delete the workspace's real ones.

    Measured 2026-08-31 before the ceiling existed:
        corrupt .git   -> prints the enclosing repo's branch, rc=0
        no .git at all -> prints the enclosing repo's branch, rc=0

    `_list_commits_env` already fenced `list_commits` and `current_head` fences
    itself — but `_git`, which every other primitive here goes through, did not.
    """
    outer = tmp_path / "outer"
    (outer / "ws" / "repo").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "enclosing", "."], cwd=outer, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=outer, check=True, env=_GIT_ENV
    )

    inner = outer / "ws" / "repo"
    (inner / ".git").mkdir()
    (inner / ".git" / "HEAD").write_text("CORRUPT\n")

    # Every read primitive must refuse, not answer with the enclosing repo.
    with pytest.raises(GitCommitError):
        list_branches(inner)
    with pytest.raises(GitCommitError):
        head_branch(inner)
    with pytest.raises(GitCommitError):
        branch_exists(inner, "enclosing")
    with pytest.raises(GitCommitError):
        has_commits(inner)


def test_the_ceiling_leaves_a_HEALTHY_repo_alone(tmp_path):
    """The control. A ceiling that broke real repos would be caught by the whole
    suite, but it must be pinned here beside the test that motivates it: the
    ceiling is the repo's PARENT, so a real repo still finds its own `.git`."""
    outer = tmp_path / "outer"
    (outer / "ws" / "repo").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "enclosing", "."], cwd=outer, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=outer, check=True, env=_GIT_ENV
    )

    inner = outer / "ws" / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=inner, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "y"], cwd=inner, check=True, env=_GIT_ENV
    )
    subprocess.run(["git", "branch", "feature"], cwd=inner, check=True)

    assert sorted(list_branches(inner)) == ["feature", "main"]
    assert head_branch(inner) == "main"
    assert branch_exists(inner, "feature")
    assert not branch_exists(inner, "enclosing"), "the enclosing repo must be invisible"


def test_a_TAG_sharing_a_branchs_name_does_not_hide_the_branch(tmp_path):
    """`%(refname:short)` shortens to the shortest UNAMBIGUOUS name, so what git
    calls a branch depends on what OTHER refs exist.

    Measured 2026-08-31 with a branch `feature` and a tag also called `feature`:

        --format='%(refname:short)'  ->  heads/feature
        show-ref --verify refs/heads/heads/feature  ->  rc=1

    So the branch reported a name its own existence check answers NO for.
    `_real_refs` filters on exactly that, so the reconciler stopped seeing the
    branch, deleted its row as an orphan, and cleared the root pointer if it
    named it.
    """
    d = _repo(tmp_path)
    subprocess.run(["git", "branch", "feature"], cwd=d, check=True)
    subprocess.run(["git", "tag", "feature"], cwd=d, check=True)

    assert sorted(list_branches(d)) == ["feature", "main"]
    # The real point: every name this returns must survive its own existence check.
    for name in list_branches(d):
        assert branch_exists(d, name), f"list_branches returned {name!r}, which it denies exists"


def test_branch_SHORTHAND_is_rejected_even_though_git_exits_zero(tmp_path, monkeypatch):
    """`--branch` EXPANDS shorthand and prints the result, so the exit code alone
    says yes to a name that means a different branch.

    Measured 2026-08-31 with `other` as the previous branch:

        git check-ref-format --branch '@{-1}'  ->  rc=0, prints "other"
        git branch '@{-1}'                     ->  acts on `other`

    A user could POST this, pass validation, and have git create or collide with
    a branch the `branches` row does not name — exactly the row/ref divergence
    this entity exists to prevent.

    `is_valid_branch_name` runs git in the PROCESS cwd, so the shorthand only
    expands when that cwd is a repo with a previous branch. Chdir'ing into one is
    what makes this test able to fail — run from anywhere else, `@{-1}` is
    rejected by the exit code alone and the assertion passes without the fix.
    """
    d = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=d, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=d, check=True)
    monkeypatch.chdir(d)

    # The premise: git really does expand it here.
    expanded = subprocess.run(
        ["git", "check-ref-format", "--branch", "@{-1}"], cwd=d, capture_output=True, text=True
    )
    assert expanded.returncode == 0 and expanded.stdout.strip() == "other", expanded

    assert not is_valid_branch_name("@{-1}")


def test_ordinary_names_still_pass_and_git_specific_ones_still_fail():
    """The control for the check above. Comparing stdout to the input must not
    start rejecting normal names, and must keep the rules `--branch` adds over
    the bare form — the bare `check-ref-format refs/heads/HEAD` exits 0."""
    for good in ("main", "feat/x", "new_ideas", "a-b.c"):
        assert is_valid_branch_name(good), good
    for bad in ("HEAD", "-", "", " x", "x ", "a..b", "a b"):
        assert not is_valid_branch_name(bad), bad


def test_a_BROKEN_ref_raises_instead_of_silently_shortening_the_list(tmp_path):
    """`git branch` exits ZERO on a corrupt ref file, warns on stderr, and omits
    that branch. Measured 2026-08-31:

        $ echo garbage > .git/refs/heads/feature
        $ git branch --format='%(refname)'
        warning: ignoring broken ref refs/heads/feature
        refs/heads/main
        rc=0

    An incomplete listing that reports success is worse than a failure:
    `backfill_branch_entities` treats every name it does not see as an ORPHAN and
    DELETES its row, clearing the root pointer if it named one. The ref file is
    still on disk — only the reader lost it.
    """
    d = _repo(tmp_path)
    subprocess.run(["git", "branch", "feature"], cwd=d, check=True)
    (d / ".git" / "refs" / "heads" / "feature").write_text("garbage\n")

    with pytest.raises(GitCommitError, match="broken ref"):
        list_branches(d)


def test_a_healthy_repo_still_lists_normally(tmp_path):
    """The control. Raising on any stderr at all would break every listing that
    git decorates with an unrelated warning, so the check is on the specific
    phrase and must not fire here."""
    d = _repo(tmp_path)
    subprocess.run(["git", "branch", "feature"], cwd=d, check=True)
    assert sorted(list_branches(d)) == ["feature", "main"]


def test_gitio_git_runs_hermetically(tmp_path):
    """`_m1` calls `backfill_branch_entities`, whose git reads come through
    `gitio._git` — so `phases/setup.py:_git_env`'s claim that "nothing an
    operator configures can introduce a hook, a signing helper or an interactive
    prompt into M1's locked region" depends on THIS environment too.
    """
    from call_manager.proposal_assist.citations.gitio import _git_env

    env = _git_env(tmp_path)
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# --- branch_tips / commit_exists (version-graph lanes) -----------------------


def _git(repo, *argv) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
    )


def _head(repo) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_branch_tips_maps_each_branch_to_its_full_sha(tmp_path):
    repo = _repo(tmp_path)
    main = _head(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "on side")
    side = _head(repo)
    _git(repo, "checkout", "-q", "main")

    assert branch_tips(repo) == {"main": main, "side": side}
    assert main != side


def test_branch_tips_is_not_confused_by_a_tag_of_the_same_name(tmp_path):
    """`%(refname:short)` answers `heads/feature` here -- the trap list_branches
    documents at length. `%(refname)` is unambiguous."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "feature")
    _git(repo, "tag", "feature")

    assert set(branch_tips(repo)) == {"main", "feature"}


def test_branch_tips_refuses_a_broken_ref_rather_than_omitting_it(tmp_path):
    """Measured 2026-08-31: `git for-each-ref` behaves exactly like `git branch`
    here -- it warns "ignoring broken ref" on stderr, exits ZERO, and omits the
    branch. A short map that reports success is worse than a failure, because
    the reconciler treats an unseen name as an orphan and deletes its row."""
    repo = _repo(tmp_path)
    _git(repo, "branch", "feature")
    (repo / ".git" / "refs" / "heads" / "feature").write_text("garbage\n")

    with pytest.raises(GitCommitError, match="broken ref"):
        branch_tips(repo)


def test_commit_exists(tmp_path):
    repo = _repo(tmp_path)
    assert commit_exists(repo, _head(repo)) is True
    assert commit_exists(repo, "0" * 40) is False


def test_commit_exists_is_false_for_a_tree_that_is_not_a_commit(tmp_path):
    """`^{commit}` is load-bearing: a bare `rev-parse --verify` accepts a TREE
    or a BLOB sha, and `git branch <name> <tree>` would then fail deep inside
    create_branch with a message no status code maps onto."""
    repo = _repo(tmp_path)
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert commit_exists(repo, tree) is False


def test_commit_exists_raises_rather_than_answering_no_outside_a_repository(tmp_path):
    """Exit 128 is "this is not a repository", not "that commit is missing".
    Collapsing it to False makes the route answer 404 and tell the user their
    commit is gone when the repository is the thing that is broken."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(GitCommitError):
        commit_exists(not_a_repo, "0" * 40)
