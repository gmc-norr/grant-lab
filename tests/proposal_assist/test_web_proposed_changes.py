import subprocess
import uuid
from pathlib import Path

from call_manager.db.models import Reference, ReferenceChunk, WorkspaceViewer
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.editing.save import save_section
from call_manager.proposal_assist.ports.stamp import stamp_for_workspace
from call_manager.proposal_assist.proposals.proposing import (
    capture_base,
    propose_citation_insert,
    propose_prose_edit,
)
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from tests.proposal_assist.conftest import make_user, make_workspace

_EMBED_DIM = 1024
_CHUNK_TEXT = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."


def _seed_citation_pending(s, ws, tmp_path):
    repo = Path(tmp_path) / str(ws.id) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\norig.\n")
    ref = Reference(
        workspace_id=ws.id,
        kind="paper",
        source_type="uploaded",
        filename="seed.pdf",
        storage_path="seed/seed.pdf",
        mime_type="application/pdf",
        citekey="smith2021",
        metadata_status="approved",
    )
    s.add(ref)
    s.flush()
    s.add(
        ReferenceChunk(
            reference_id=ref.id,
            workspace_id=ws.id,
            chunk_index=0,
            section_path="Results",
            text=_CHUNK_TEXT,
            embedding=[0.0] * _EMBED_DIM,
            embedding_model="fake-embedder",
        )
    )
    s.flush()
    base_commit, base_section = capture_base(repo, 1)
    pc = propose_citation_insert(
        session=s,
        workspace_id=ws.id,
        section_index=1,
        base_commit_sha=base_commit,
        base_section_sha=base_section,
        reference_id=str(ref.id),
        anchor="orig.",
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
        origin_kind="interactive_turn",
    )
    return pc


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _seed_pending(s, ws, tmp_path):
    repo = Path(tmp_path) / str(ws.id) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\norig.\n")
    base_commit, base_section = capture_base(repo, 1)
    pc = propose_prose_edit(
        session=s,
        workspace_id=ws.id,
        section_index=1,
        new_text="## Methods\nnew.\n",
        base_commit_sha=base_commit,
        base_section_sha=base_section,
        origin_kind="interactive_turn",
    )
    return pc


def test_accept_endpoint_applies_change(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert "new." in (Path(tmp_path) / ws_id / "repo" / PROPOSAL_FILENAME).read_text()


def test_list_endpoint_returns_pending(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        _seed_pending(s, ws, tmp_path)
        s.commit()
        ws_id = str(ws.id)

    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "pending"


def test_reject_endpoint_flips_status(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_accept_endpoint_rejects_non_member(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # The workspace is owned by a stranger; current_user (the authed client) is
    # not a member, so the member gate must reject with 403.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=stranger)
        s.flush()
        pc = _seed_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 403


def test_list_returns_empty_for_unset_up_workspace(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # Workspace exists but Phase-0 hasn't written proposal.md yet (no repo on
    # disk). The list endpoint must NOT 500 — it returns an empty collection.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = str(ws.id)

    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_list_excludes_non_pending(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # Only pending rows surface in the list; an already-resolved row is excluded.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        repo = Path(tmp_path) / str(ws.id) / "repo"
        _init_repo(repo, "preamble\n\n## Methods\norig.\n")
        base_commit, base_section = capture_base(repo, 1)
        propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\npending.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        resolved = propose_prose_edit(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            new_text="## Methods\nresolved.\n",
            base_commit_sha=base_commit,
            base_section_sha=base_section,
            origin_kind="interactive_turn",
        )
        resolved.status = "rejected"
        s.commit()
        ws_id = str(ws.id)

    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "pending"


def test_accept_already_resolved_returns_409(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # Accepting a row that is no longer pending (here: already accepted) is a 409.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    first = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert first.status_code == 200
    second = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert second.status_code == 409


def test_accept_cross_workspace_returns_404(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # Defense-in-depth at the web layer: current_user owns BOTH workspaces (so the
    # member gate passes for ws_b), but the proposal belongs to ws_a. Accepting it
    # under ws_b's path must 404 — the service scopes by (id, workspace_id).
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=current_user)
        ws_b = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_pending(s, ws_a, tmp_path)
        s.commit()
        pc_id, ws_b_id = str(pc.id), str(ws_b.id)

    resp = client_authed_owner.post(f"/api/workspaces/{ws_b_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 404


def test_accept_endpoint_allows_viewer(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # The gate is MEMBERSHIP, not ownership: a non-owner workspace member (a
    # WorkspaceViewer collaborator) can accept. current_user is the viewer here.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="ws-owner@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        pc = _seed_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_reject_endpoint_rejects_non_member(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-r@ki.se")
        ws = make_workspace(s, owner=stranger)
        s.flush()
        pc = _seed_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/reject")
    assert resp.status_code == 403


def test_list_endpoint_rejects_non_member(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-l@ki.se")
        ws = make_workspace(s, owner=stranger)
        s.flush()
        _seed_pending(s, ws, tmp_path)
        s.commit()
        ws_id = str(ws.id)

    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes")
    assert resp.status_code == 403


def _seed_pending_then_move_section(s, ws, current_user, tmp_path):
    # Propose against the section, then have the owner move the SAME section so the
    # proposal's base_section_sha is now out of date.
    repo = Path(tmp_path) / str(ws.id) / "repo"
    _init_repo(repo, "preamble\n\n## Methods\norig.\n")
    base_commit, base_section = capture_base(repo, 1)
    pc = propose_prose_edit(
        session=s,
        workspace_id=ws.id,
        section_index=1,
        new_text="## Methods\nagent.\n",
        base_commit_sha=base_commit,
        base_section_sha=base_section,
        origin_kind="interactive_turn",
    )
    save_section(
        session=s,
        workspace=ws,
        stamp=stamp_for_workspace(ws),
        user_id=current_user.id,
        section_index=1,
        new_text="## Methods\nhuman.\n",
        base_commit_sha=base_commit,
        base_section_sha=base_section,
        workspaces_dir=str(tmp_path),
        base_branch=None,
    )
    return pc


def test_accept_endpoint_goes_stale_when_section_moved(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # The section moved after the proposer's read: accept must return 200 with
    # status "stale" (NOT an error) and must NOT overwrite the human edit.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_pending_then_move_section(s, ws, current_user, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stale"
    text = (Path(tmp_path) / ws_id / "repo" / PROPOSAL_FILENAME).read_text()
    assert "human." in text
    assert "agent." not in text


def test_list_omits_stale_after_section_moves(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # The list runs refresh_staleness, so a proposal whose section has moved is
    # flipped to stale and drops out of the pending-only list.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        _seed_pending_then_move_section(s, ws, current_user, tmp_path)
        s.commit()
        ws_id = str(ws.id)

    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_accept_citation_endpoint_applies_marker_and_ledger(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_citation_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)

    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    repo = Path(tmp_path) / ws_id / "repo"
    proposal_text = (repo / PROPOSAL_FILENAME).read_text()
    markers = find_markers(proposal_text)
    assert len(markers) == 1
    assert markers[0]["citekey"] == "smith2021"
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    assert len(ledger["evidence"]) == 1


def _seed_spec_pending(s, ws, tmp_path, locked=False):
    from datetime import UTC, datetime

    from call_manager.proposal_assist.artifacts import Artifact

    repo = Path(tmp_path) / str(ws.id) / "repo"
    if not (repo / ".git").exists():
        # Standalone: create the repo + the draft file so the section-1 layout
        # matches what capture_base(SPEC) expects.
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "proposal.md").write_text("preamble\n")
    # Add the spec artifact to the (possibly pre-existing) repo and commit.
    (repo / "proposal-spec.md").write_text("## Aims\norig.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "spec init"], cwd=repo, check=True)
    if locked:
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = ws.owner_id
        s.flush()
    base_commit, base_section = capture_base(repo, 1, artifact=Artifact.SPEC)
    return propose_prose_edit(
        session=s,
        workspace_id=ws.id,
        section_index=1,
        new_text="## Aims\nagent.\n",
        base_commit_sha=base_commit,
        base_section_sha=base_section,
        origin_kind="interactive_turn",
        target_artifact="spec",
    )


def test_accept_spec_endpoint_commits(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_spec_pending(s, ws, tmp_path)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)
    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert "agent." in (Path(tmp_path) / ws_id / "repo" / "proposal-spec.md").read_text()


def test_accept_spec_locked_returns_409(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        pc = _seed_spec_pending(s, ws, tmp_path, locked=True)
        s.commit()
        pc_id, ws_id = str(pc.id), str(ws.id)
    resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    with pg_session_factory() as s:
        from call_manager.db.models import ProposedChange

        assert s.get(ProposedChange, uuid.UUID(pc_id)).status == "pending"


def test_list_filters_by_target_artifact(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        _seed_pending(s, ws, tmp_path)  # draft (writes proposal.md repo)
        _seed_spec_pending(s, ws, tmp_path)  # spec (adds proposal-spec.md)
        s.commit()
        ws_id = str(ws.id)
    spec_only = client_authed_owner.get(
        f"/api/workspaces/{ws_id}/proposed-changes?target_artifact=spec"
    ).json()["items"]
    assert [i["target_artifact"] for i in spec_only] == ["spec"]
    assert spec_only[0]["new_text"] == "## Aims\nagent.\n"
    all_items = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes").json()["items"]
    assert {i["target_artifact"] for i in all_items} == {"draft", "spec"}
