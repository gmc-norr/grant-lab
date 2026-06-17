import subprocess
from pathlib import Path

from call_manager.proposal_assist.proposals.proposing import capture_base, propose_prose_edit
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

    resp = client_authed_owner.post(f"/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
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

    resp = client_authed_owner.get(f"/workspaces/{ws_id}/proposed-changes")
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

    resp = client_authed_owner.post(f"/workspaces/{ws_id}/proposed-changes/{pc_id}/reject")
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

    resp = client_authed_owner.post(f"/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
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

    resp = client_authed_owner.get(f"/workspaces/{ws_id}/proposed-changes")
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

    resp = client_authed_owner.get(f"/workspaces/{ws_id}/proposed-changes")
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

    first = client_authed_owner.post(f"/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
    assert first.status_code == 200
    second = client_authed_owner.post(f"/workspaces/{ws_id}/proposed-changes/{pc_id}/accept")
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

    resp = client_authed_owner.post(f"/workspaces/{ws_b_id}/proposed-changes/{pc_id}/accept")
    assert resp.status_code == 404
