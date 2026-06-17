import subprocess
from pathlib import Path

import pytest

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.proposals.accepting import (
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    accept_proposed_change,
)
from call_manager.proposal_assist.proposals.proposing import (
    capture_base,
    propose_prose_edit,
)
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _setup(s, tmp_path):
    ws = make_workspace(s)
    s.flush()
    repo = Path(tmp_path) / str(ws.id) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\noriginal.\n")
    return ws, repo


def test_accept_prose_applies_and_records_approver(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nrewritten.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s,
            proposed_change_id=pc.id,
            workspace_id=ws.id,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        s.flush()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resolved_by == approver.id
        assert got.resulting_commit_sha
        assert "rewritten." in (repo / PROPOSAL_FILENAME).read_text()


def test_accept_non_pending_is_rejected(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nrewritten.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        pc.status = "rejected"
        s.flush()
        with pytest.raises(ProposedChangeNotPending):
            accept_proposed_change(
                session=s,
                proposed_change_id=pc.id,
                workspace_id=ws.id,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )


def test_accept_does_not_clobber_human_edit_race(pg_session_factory, tmp_path):
    # Proposer READS at the original base; a human edits the SAME section before
    # the proposal is accepted. Accept must go stale, not overwrite (finding #1).
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()

        base_commit, base_section = capture_base(repo, 1)  # T0: proposer's read

        # T1: a human edits the section and commits — section sha moves.
        save_section(
            session=s,
            workspace=ws,
            user_id=ws.owner_id,
            section_index=1,
            new_text="## Methods\nhuman-edited.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            workspaces_dir=str(tmp_path),
        )
        s.flush()

        # T2: proposer proposes using the now-stale T0 snapshot.
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nagent-version.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s,
            proposed_change_id=pc.id,
            workspace_id=ws.id,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        s.flush()

        assert s.get(ProposedChange, pc.id).status == "stale"
        text = (repo / PROPOSAL_FILENAME).read_text()
        assert "human-edited." in text  # human edit preserved
        assert "agent-version." not in text  # agent proposal NOT applied


def test_accept_rejects_mismatched_workspace(pg_session_factory, tmp_path):
    # A member of workspace B cannot accept a proposed_change from workspace A
    # by calling it under B's path (finding #2).
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        other_owner = make_user(s, email="other-owner@ki.se")
        other = make_workspace(s, owner=other_owner)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        s.flush()
        with pytest.raises(ProposedChangeNotFound):
            accept_proposed_change(
                session=s,
                proposed_change_id=pc.id,
                workspace_id=other.id,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )


def test_accept_noop_proposal_is_accepted_at_current_head(pg_session_factory, tmp_path):
    # A no-op proposal (new_text == live section text, base unmoved) makes
    # save_section short-circuit (changed=False, no new commit). The proposal is
    # still "accepted" and resulting_commit_sha is the unchanged HEAD (pins the
    # return contract for the changed=False path).
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        live_text = split_sections((repo / PROPOSAL_FILENAME).read_text())[1].text
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text=live_text,
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        s.flush()

        accept_proposed_change(
            session=s,
            proposed_change_id=pc.id,
            workspace_id=ws.id,
            approver=approver,
            workspaces_dir=str(tmp_path),
        )
        s.flush()

        got = s.get(ProposedChange, pc.id)
        assert got.status == "accepted"
        assert got.resulting_commit_sha == base_commit  # HEAD did not move
        assert "original." in (repo / PROPOSAL_FILENAME).read_text()


def test_accept_rejects_spec_target(pg_session_factory, tmp_path):
    # SP2a accepts the draft path only; a spec-target row is deferred to SP2b.
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        pc.target_artifact = "spec"
        s.flush()
        with pytest.raises(NotImplementedError):
            accept_proposed_change(
                session=s,
                proposed_change_id=pc.id,
                workspace_id=ws.id,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )


def test_accept_rejects_non_prose_kind(pg_session_factory, tmp_path):
    # SP2a accepts prose_edit only; citation kinds land in a later SP2a plan.
    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        pc.kind = "citation_insert"
        s.flush()
        with pytest.raises(NotImplementedError):
            accept_proposed_change(
                session=s,
                proposed_change_id=pc.id,
                workspace_id=ws.id,
                approver=approver,
                workspaces_dir=str(tmp_path),
            )


def test_reject_sets_status_and_approver(pg_session_factory, tmp_path):
    from call_manager.proposal_assist.proposals.accepting import reject_proposed_change

    with pg_session_factory() as s:
        ws, repo = _setup(s, tmp_path)
        approver = make_user(s, email="approver@ki.se")
        s.flush()
        base_commit, base_section = capture_base(repo, 1)
        pc = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nx.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        s.flush()
        reject_proposed_change(
            session=s, proposed_change_id=pc.id, workspace_id=ws.id, approver=approver
        )
        s.flush()
        got = s.get(ProposedChange, pc.id)
        assert got.status == "rejected"
        assert got.resolved_by == approver.id
        assert got.resolved_at is not None
