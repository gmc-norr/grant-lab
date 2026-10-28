"""Create and delete. Both take the mutex; both refuse loudly rather than guess."""

import subprocess

import pytest

from grant_lab.proposal_assist.branches import (
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
from grant_lab.proposal_assist.citations.gitio import (
    branch_exists,
    branch_tips,
    current_branch,
    current_head,
)
from grant_lab.proposal_assist.collab import state as collab_state
from grant_lab.proposal_assist.collab import store
from grant_lab.proposal_assist.collab.markdown_bridge import markdown_to_pm
from grant_lab.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.ports.artifacts import PROPOSAL_FILENAME
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


# --- from_sha=None founds the branch on the LIVE doc (review 2026-10-26 M-PA-8) ---


def _seed_authored_draft(pg_session_factory, wid: int, actor_id: int, md: str) -> None:
    """One AUTHORED log row holding *md*, built through the real bridge — the
    un-checkpointed keystrokes a branch create has to capture. Authored on
    purpose: `checkpoint_artifact` skips an unauthored (hydration-seed) tail
    without committing, so an actor_id=None row would make the assertion below
    pass for the wrong reason."""
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=doc.get_update()
        )
        s.commit()


def _show(repo, ref: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{PROPOSAL_FILENAME}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_create_at_head_founds_the_branch_on_the_live_doc(pg_session_factory, tmp_path):
    """M-PA-8 (review 2026-10-26, appendix A6). `git branch <name> HEAD` founds
    the ref on HEAD, and HEAD is only ever the last CHECKPOINT. Ten minutes of
    keystrokes that live in the Y.Doc log and nowhere else were therefore absent
    from every new branch, and switching to it made the recent work look lost.

    So a create with no explicit start point checkpoints the live document
    first — under the same mutex, with the same call the switch uses."""
    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        s.commit()
        wid, oid = ws.id, ws.owner_id
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, "# Aims\n\nbody\n")
    checkpointed_before = current_head(repo)
    _seed_authored_draft(
        pg_session_factory, wid, oid, "# Aims\n\nbody\n\nTEN MINUTES OF NEW WORK\n"
    )
    # The precondition, pinned: git does NOT hold the new work yet. Without this
    # the assertions below would also pass against a repo that was never stale.
    assert "TEN MINUTES OF NEW WORK" not in _show(repo, "HEAD")

    with pg_session_factory() as s:
        create_workspace_branch(
            s, stamp=stamp_for(s, wid), workspace_id=wid, name="alt", workspaces_dir=tmp_path
        )
        s.commit()

    assert "TEN MINUTES OF NEW WORK" in _show(repo, "alt")
    # ...because the checkpoint committed on the branch being left, and the new
    # ref was founded on THAT commit — not on a detached copy of the log.
    assert current_head(repo) != checkpointed_before
    assert branch_tips(repo)["alt"] == current_head(repo)
    assert current_branch(repo) == "main"  # a create still never moves the user


def test_create_from_an_explicit_sha_does_not_checkpoint(pg_session_factory, tmp_path):
    """The control for the guard above. An explicit `from_sha` is the user
    pinning a commit they are looking at, so the live document must NOT be
    checkpointed: that would both move HEAD on the branch they are on and
    silently flush un-saved keystrokes out of an operation whose contract says
    it changes nothing anybody can see."""
    with pg_session_factory() as s:
        ws = make_workspace(s, title="w")
        s.commit()
        wid, oid = ws.id, ws.owner_id
    repo = workspace_repo_dir(tmp_path, wid)
    init_workspace_repo(repo, "# Aims\n\nbody\n")
    pinned = current_head(repo)
    _seed_authored_draft(
        pg_session_factory, wid, oid, "# Aims\n\nbody\n\nTEN MINUTES OF NEW WORK\n"
    )

    with pg_session_factory() as s:
        create_workspace_branch(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            name="pinned",
            workspaces_dir=tmp_path,
            from_sha=pinned,
        )
        s.commit()

    assert branch_tips(repo)["pinned"] == pinned
    assert current_head(repo) == pinned  # no checkpoint commit happened
    assert "TEN MINUTES OF NEW WORK" not in _show(repo, "HEAD")
    with pg_session_factory() as s:
        # and the live log is untouched, watermark included
        assert len(store.load_updates(s, workspace_id=wid, artifact="draft")) == 1
        assert collab_state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0
