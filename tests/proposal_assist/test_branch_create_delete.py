"""Create and delete. Both take the mutex; both refuse loudly rather than guess."""

import subprocess

import pytest

from call_manager.proposal_assist.branches import (
    BranchExists,
    BranchInUse,
    BranchLast,
    BranchNameInvalid,
    BranchNotFound,
    BranchStartUnknown,
    create_workspace_branch,
    delete_workspace_branch,
    list_workspace_branches,
)
from call_manager.proposal_assist.citations.gitio import (
    branch_exists,
    branch_tips,
    current_branch,
    current_head,
)
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_workspace, stamp_for


def _ws(session_factory, tmp_path, *, commit=True):
    with session_factory() as s:
        wid = make_workspace(s, title="w").id
        s.commit()
    repo = workspace_repo_dir(tmp_path, wid)
    if commit:
        init_workspace_repo(repo, "# Aims\n\nbody\n")
    else:
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo, check=True)
    return wid, repo


def test_create_makes_the_branch_without_moving_head(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    head_before = current_head(repo)
    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="aims-ambitious",
            workspaces_dir=tmp_path,
        )
        s.commit()
    assert branch_exists(repo, "aims-ambitious")
    assert current_branch(repo) == "main"
    assert current_head(repo) == head_before


def test_create_does_not_disturb_the_log(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x00\x00"
        )
        s.commit()
    with pg_session_factory() as s:
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="x", workspaces_dir=tmp_path
        )
        s.commit()
    with pg_session_factory() as s:
        assert len(store.load_updates(s, workspace_id=wid, artifact="draft")) == 1


@pytest.mark.parametrize("bad", ["a..b", "foo.lock", "-lead", "with space", ""])
def test_create_rejects_a_name_git_rejects(pg_session_factory, tmp_path, bad):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises(BranchNameInvalid):
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name=bad, workspaces_dir=tmp_path
        )
    # and leaves no partially created ref. This assertion is OUTSIDE the
    # pytest.raises block on purpose: inside it, the raise from
    # create_workspace_branch exits the with-block immediately and the assert
    # never runs — a dead gate. Out here it actually verifies no ref leaked.
    with pg_session_factory() as s:
        assert sorted(list_workspace_branches(s, workspace_id=wid, workspaces_dir=tmp_path)[0]) == [
            "main"
        ]


def test_create_refuses_a_duplicate(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="dup", workspaces_dir=tmp_path
        )
        s.commit()
    with pg_session_factory() as s, pytest.raises(BranchExists):
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="dup", workspaces_dir=tmp_path
        )


def test_create_refuses_on_a_repo_with_no_commits(pg_session_factory, tmp_path):
    # A workspace still in setup. Measured: `git branch x HEAD` exits 128.
    wid, repo = _ws(pg_session_factory, tmp_path, commit=False)
    with pg_session_factory() as s, pytest.raises(BranchNotFound):
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="x", workspaces_dir=tmp_path
        )


def test_delete_removes_a_non_current_branch(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="dead", workspaces_dir=tmp_path
        )
        s.commit()
    with pg_session_factory() as s:
        delete_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="dead", workspaces_dir=tmp_path
        )
        s.commit()
    assert branch_exists(repo, "dead") is False


def test_delete_refuses_the_CURRENT_branch(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="other", workspaces_dir=tmp_path
        )
        s.commit()
    with pg_session_factory() as s, pytest.raises(BranchInUse):
        delete_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="main", workspaces_dir=tmp_path
        )
    assert branch_exists(repo, "main") is True


def test_delete_refuses_the_LAST_branch(pg_session_factory, tmp_path):
    # A repo with no branch has a detached HEAD, and §4.1's reconciliation then
    # has nothing to reconcile to.
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises((BranchInUse, BranchLast)):
        delete_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="main", workspaces_dir=tmp_path
        )
    assert branch_exists(repo, "main") is True


def test_delete_of_a_missing_branch_raises(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises(BranchNotFound):
        delete_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="ghost", workspaces_dir=tmp_path
        )


def test_list_returns_the_names_and_the_current_one(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="b", workspaces_dir=tmp_path
        )
        s.commit()
    with pg_session_factory() as s:
        names, current, _tips = list_workspace_branches(
            s, workspace_id=wid, workspaces_dir=tmp_path
        )
    assert sorted(names) == ["b", "main"]
    assert current == "main"


# --- from_sha: branch from a chosen commit ----------------------------------


def _extra_commit(repo, body: str, message: str) -> str:
    (repo / "proposal.md").write_text(body, encoding="utf-8")
    for argv in (["add", "proposal.md"], ["commit", "-q", "-m", message]):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
        )
    return current_head(repo)


def test_create_from_an_explicit_sha_puts_the_branch_there(pg_session_factory, tmp_path):
    """The whole point of the feature: branch from the version you are looking
    at, not from wherever HEAD happens to be."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    older = current_head(repo)
    newer = _extra_commit(repo, "# Aims\n\nmore\n", "newer")
    assert older != newer

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="from-older",
            workspaces_dir=tmp_path,
            from_sha=older,
        )
        s.commit()

    assert branch_tips(repo)["from-older"] == older
    # And HEAD did not move, so this is not just "it branched at HEAD".
    assert branch_tips(repo)["main"] == newer


def test_create_with_no_sha_still_branches_at_head(pg_session_factory, tmp_path):
    """The control. Without it, an implementation that always used from_sha and
    crashed on None would pass every other test here, because they all pass a
    sha."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    _extra_commit(repo, "# Aims\n\nmore\n", "newer")
    head = current_head(repo)

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="at-head",
            workspaces_dir=tmp_path,
        )
        s.commit()

    assert branch_tips(repo)["at-head"] == head


def test_create_from_a_sha_that_is_not_in_this_repo_is_refused(pg_session_factory, tmp_path):
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises(BranchStartUnknown):
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="ghost",
            workspaces_dir=tmp_path,
            from_sha="0" * 40,
        )
    assert "ghost" not in branch_tips(repo)


def test_create_from_a_non_sha_is_refused_before_git_sees_it(pg_session_factory, tmp_path):
    """`--output=/tmp/pwned` is not a sha. git parses options before it reaches
    any `--` separator, so an unvalidated value in argv is an option-injection
    vector -- the reason ports/shas.py exists."""
    wid, repo = _ws(pg_session_factory, tmp_path)
    with pg_session_factory() as s, pytest.raises(BranchStartUnknown):
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="evil",
            workspaces_dir=tmp_path,
            from_sha="--output=/tmp/pwned",
        )
    assert "evil" not in branch_tips(repo)
