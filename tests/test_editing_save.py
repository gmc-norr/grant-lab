"""save_section — the single write path for user edits of proposal.md.

Uses the production repo layout: <workspaces_dir>/<workspace_id>/repo.
"""

import subprocess
from datetime import UTC, datetime

import pytest

from grant_lab.db.models import (
    ArtifactVersion,
    ClaimAnalysisJob,
    TurnJob,
    User,
    Workspace,
    WorkspaceEvent,
)
from grant_lab.proposal_assist.artifacts import Artifact
from grant_lab.proposal_assist.citations.gitio import _git_env, current_head
from grant_lab.proposal_assist.editing.save import (
    BranchMismatchError,
    SaveResult,
    SectionStructureError,
    SpecLockedError,
    StaleSectionError,
    save_section,
)
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir
from grant_lab.proposal_assist.ports.stamp import stamp_for_workspace
from tests.proposal_assist.conftest import make_user, make_workspace

DOC = "# T\nintro\n## Background\nold body [@x9]\n## Aims\naims\n"


def _init_repo(tmp_path, ws_id, doc=DOC):
    """Create <tmp_path>/<ws_id>/repo with proposal.md committed (production layout)."""
    repo = workspace_repo_dir(tmp_path, ws_id)
    repo.mkdir(parents=True)
    (repo / "proposal.md").write_text(doc)
    env = _git_env()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo, check=True, env=env
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=env)
    return repo


def _ws(session):
    user = User(email="owner@example.org", is_admin=False)
    session.add(user)
    session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws)
    session.flush()
    return ws, user


def _section_sha(repo, i):
    return split_sections((repo / "proposal.md").read_text())[i].sha


def test_happy_path_save(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        head = current_head(repo)
        res = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=user.id,
            section_index=1,
            new_text="## Background\nnew body\n",
            base_commit_sha=head,
            base_section_sha=_section_sha(repo, 1),
            workspaces_dir=tmp_path,
            base_branch=None,
        )
        s.commit()

        assert isinstance(res, SaveResult)
        assert res.changed is True
        assert res.commit_sha == current_head(repo) != head
        assert res.head == res.commit_sha  # new HEAD == the commit just made
        expected = "# T\nintro\n## Background\nnew body\n## Aims\naims\n"
        assert res.section_sha == split_sections(expected)[1].sha
        assert (repo / "proposal.md").read_text() == expected

        av = s.query(ArtifactVersion).one()
        assert av.created_by == "user"
        assert av.commit_sha == res.commit_sha

        ev = s.query(WorkspaceEvent).one()
        assert ev.kind == "user_edit"
        assert ev.actor_id == user.id
        assert ev.payload["section_index"] == 1
        assert ev.payload["commit_sha"] == res.commit_sha

        job = s.query(ClaimAnalysisJob).one()
        assert job.status == "queued"
        assert job.section_index == 1
        assert job.section_sha == split_sections(expected)[1].sha
        assert job.commit_sha == res.commit_sha
        assert res.job_enqueued is True


def test_turn_in_flight_no_longer_blocks_save(pg_session_factory, tmp_path):
    """save_section must succeed even when an agent turn is queued/running.

    The workspace-wide turn-freeze was removed: the agent no longer commits
    directly to proposal.md, so there is no git-level race to prevent.
    Concurrency is now guarded by the commit mutex + base_section_sha staleness.
    """
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        head = current_head(repo)
        s.add(TurnJob(workspace_id=ws.id, status="queued"))
        s.flush()
        result = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=user.id,
            section_index=1,
            new_text="## Background\nx\n",
            base_commit_sha=head,
            base_section_sha=_section_sha(repo, 1),
            workspaces_dir=tmp_path,
            base_branch=None,
        )
        assert result.commit_sha  # no TurnInFlightError raised


def test_stale_section_rejected_but_unrelated_head_move_allowed(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        old_head = current_head(repo)
        old_s1 = _section_sha(repo, 1)
        old_s2 = _section_sha(repo, 2)

        # Simulate an agent commit that changes section 2 only.
        env = _git_env()
        (repo / "proposal.md").write_text(DOC.replace("aims\n", "agent-updated aims\n"))
        subprocess.run(["git", "commit", "-aqm", "agent edit"], cwd=repo, check=True, env=env)

        # HEAD moved, but section 1 is byte-identical → save proceeds.
        res = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=user.id,
            section_index=1,
            new_text="## Background\nnew body\n",
            base_commit_sha=old_head,
            base_section_sha=old_s1,
            workspaces_dir=tmp_path,
            base_branch=None,
        )
        s.commit()
        assert res.changed is True
        assert "new body" in (repo / "proposal.md").read_text()

        # HEAD moved AND section 2 changed underneath → stale.
        with pytest.raises(StaleSectionError):
            save_section(
                session=s,
                workspace=ws,
                stamp=stamp_for_workspace(ws),
                user_id=user.id,
                section_index=2,
                new_text="## Aims\nuser aims\n",
                base_commit_sha=old_head,
                base_section_sha=old_s2,
                workspaces_dir=tmp_path,
                base_branch=None,
            )
        s.rollback()


def test_a_form_loaded_on_another_branch_cannot_write_here(pg_session_factory, tmp_path):
    """A save carries the branch it was loaded from, and is refused if it moved.

    `test_stale_section_rejected_but_unrelated_head_move_allowed` above pins the
    intended SAME-BRANCH rule: HEAD may move as long as THIS section's bytes did
    not. Across branches that rule is wrong, and the two sha assertions below say
    why in the strongest form available: the head guard fires, the section guard
    does NOT, and because save.py's stale check is an AND that combination is a
    pass. A branch made from HEAD has every section byte-identical -- branches.py
    step 7 calls that the normal case right after branching -- so the shas alone
    can never see this. Measured 2026-09-01: before `base_branch` existed, this
    exact call succeeded and put the source branch's text in the target branch's
    proposal.md, beside that branch's own content.

    The DB column is moved as well as the worktree, because that is what a real
    switch does and it is the column the guard reads.
    """
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        env = _git_env()
        source_branch = ws.current_branch
        source_head = current_head(repo)
        section1_sha = _section_sha(repo, 1)  # what the edit form was rendered from

        # A branch off the same commit: every section byte-identical.
        subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=repo, check=True, env=env)
        # Move the TARGET's head by touching a DIFFERENT section, so the two
        # branches have different heads while section 1 still matches on both.
        (repo / "proposal.md").write_text(DOC.replace("aims\n", "other-branch aims\n"))
        subprocess.run(
            ["git", "commit", "-aqm", "other branch edit"], cwd=repo, check=True, env=env
        )
        ws.current_branch = "other"
        s.flush()
        assert current_head(repo) != source_head  # the head guard WILL fire
        assert _section_sha(repo, 1) == section1_sha  # ...and the section guard will not

        with pytest.raises(BranchMismatchError) as caught:
            save_section(
                session=s,
                workspace=ws,
                stamp=stamp_for_workspace(ws),
                user_id=user.id,
                section_index=1,
                new_text="## Background\nLEAKED FROM THE SOURCE BRANCH\n",
                base_commit_sha=source_head,
                base_section_sha=section1_sha,
                workspaces_dir=tmp_path,
                base_branch=source_branch,
            )
        # Both names, because the UI shows both.
        assert caught.value.base_branch == source_branch
        assert caught.value.current_branch == "other"
        # Refused BEFORE any git side-effect: the target branch is untouched.
        text = (repo / "proposal.md").read_text()
        assert "LEAKED FROM THE SOURCE BRANCH" not in text
        assert "other-branch aims" in text
        s.rollback()


def test_tier1_flags_bare_marker_and_missing_newline_glue(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        head = current_head(repo)
        res = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=user.id,
            section_index=1,
            new_text="## Background\nclaim [@hand2024]",  # no trailing newline
            base_commit_sha=head,
            base_section_sha=_section_sha(repo, 1),
            workspaces_dir=tmp_path,
            base_branch=None,
        )
        s.commit()

        bare = [i for i in res.issues if i["type"] == "bare_marker"]
        assert any(i["citekey"] == "hand2024" for i in bare)
        assert all(i["severity"] == "error" for i in bare)

        # Newline glue: the next heading must still start on its own line.
        text_now = (repo / "proposal.md").read_text()
        assert "claim [@hand2024]\n## Aims\naims\n" in text_now


def test_no_change_save_short_circuits(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        head = current_head(repo)
        live = split_sections(DOC)[1]
        res = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=user.id,
            section_index=1,
            new_text=live.text,
            base_commit_sha=head,
            base_section_sha=live.sha,
            workspaces_dir=tmp_path,
            base_branch=None,
        )
        s.commit()

        assert res.changed is False
        assert res.commit_sha == head == current_head(repo)
        assert res.head == head  # HEAD unchanged on a no-op
        assert res.section_sha == _section_sha(repo, 1)  # base sha preserved
        assert res.job_enqueued is False
        # Tier-1 still ran on the current doc (DOC contains the bare [@x9]).
        assert any(i["type"] == "bare_marker" for i in res.issues)
        assert s.query(ArtifactVersion).count() == 0
        assert s.query(WorkspaceEvent).count() == 0
        assert s.query(ClaimAnalysisJob).count() == 0
        assert (repo / "proposal.md").read_text() == DOC


def test_heading_deletion_rejected_middle_section(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        head = current_head(repo)
        with pytest.raises(SectionStructureError):
            save_section(
                session=s,
                workspace=ws,
                stamp=stamp_for_workspace(ws),
                user_id=user.id,
                section_index=1,
                new_text="no heading anymore\n",
                base_commit_sha=head,
                base_section_sha=_section_sha(repo, 1),
                workspaces_dir=tmp_path,
                base_branch=None,
            )
        # nothing committed, nothing enqueued
        assert current_head(repo) == head
        s.rollback()
        assert s.query(ClaimAnalysisJob).count() == 0


def test_preamble_gaining_heading_rejected(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        head = current_head(repo)
        with pytest.raises(SectionStructureError):
            save_section(
                session=s,
                workspace=ws,
                stamp=stamp_for_workspace(ws),
                user_id=user.id,
                section_index=0,
                new_text="## Sneaky\nx\n",
                base_commit_sha=head,
                base_section_sha=_section_sha(repo, 0),
                workspaces_dir=tmp_path,
                base_branch=None,
            )


def test_heading_rename_allowed(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        result = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=user.id,
            section_index=1,
            new_text="## Background & Significance\nbody\n",
            base_commit_sha=current_head(repo),
            base_section_sha=_section_sha(repo, 1),
            workspaces_dir=tmp_path,
            base_branch=None,
        )
        s.commit()
        assert result.changed is True
        assert "## Background & Significance" in (repo / "proposal.md").read_text()


def test_extra_heading_split_allowed(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, user = _ws(s)
        repo = _init_repo(tmp_path, ws.id)
        result = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=user.id,
            section_index=1,
            new_text="## Background\nb1\n## New Sub\nb2\n",
            base_commit_sha=current_head(repo),
            base_section_sha=_section_sha(repo, 1),
            workspaces_dir=tmp_path,
            base_branch=None,
        )
        s.commit()
        assert result.changed is True and result.job_enqueued is True
        job = s.query(ClaimAnalysisJob).one()
        assert job.section_heading == "Background"  # first chunk enqueued


def _add_spec(repo, doc):
    """Write + commit proposal-spec.md into the already-initialized repo
    (mirrors tests/test_api_spec.py::_add_spec)."""
    (repo / "proposal-spec.md").write_text(doc)
    env = _git_env()
    subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "seed spec"], cwd=repo, check=True, env=env)


def test_save_section_blocks_locked_spec(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = owner.id
        s.commit()
        repo = _init_repo(tmp_path, ws.id)
        _add_spec(repo, "## Aims\nbody.\n")
        with pytest.raises(SpecLockedError):
            save_section(
                session=s,
                workspace=ws,
                stamp=stamp_for_workspace(ws),
                user_id=owner.id,
                section_index=1,
                new_text="## Aims\nedited.\n",
                base_commit_sha="0" * 40,
                base_section_sha="0" * 64,
                workspaces_dir=tmp_path,
                artifact=Artifact.SPEC,
                base_branch=None,
            )


def test_save_section_allows_draft_while_spec_locked(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = owner.id
        s.commit()
        repo = _init_repo(tmp_path, ws.id, doc="preamble\n\n## Methods\nm.\n")
        draft = save_section(
            session=s,
            workspace=ws,
            stamp=stamp_for_workspace(ws),
            user_id=owner.id,
            section_index=1,
            new_text="## Methods\nm2.\n",
            base_commit_sha=current_head(repo),
            base_section_sha=_section_sha(repo, 1),
            workspaces_dir=tmp_path,
            artifact=Artifact.DRAFT,
            base_branch=None,
        )
        s.commit()
        assert draft.changed is True  # draft is unaffected by a spec lock
