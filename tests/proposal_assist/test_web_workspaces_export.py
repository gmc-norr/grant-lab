import subprocess
from pathlib import Path


def _seed_repo(workspaces_dir: Path, wsid, content: str) -> None:
    repo_dir = workspaces_dir / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def test_export_returns_proposal_md_bytes(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    from tests.proposal_assist.conftest import make_workspace

    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "MyProposal"
        s.commit()
        wsid = ws.id
    _seed_repo(tmp_path, wsid, "# Title\n\nBody.")

    response = client_authed_owner.get(f"/workspaces/{wsid}/export.md")
    assert response.status_code == 200
    assert response.text == "# Title\n\nBody."
    # Content-Type is markdown.
    assert "markdown" in response.headers["content-type"]
    # Content-Disposition triggers download with the workspace title.
    cd = response.headers["content-disposition"]
    assert "attachment" in cd
    assert "MyProposal" in cd


def test_export_404_when_no_proposal_yet(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """A workspace with no proposal.md (Phase 0 didn't run) returns 404."""
    from tests.proposal_assist.conftest import make_workspace

    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.get(f"/workspaces/{wsid}/export.md")
    assert response.status_code == 404


def test_export_allows_viewer(
    client_authed_user,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """Viewers can export (read-only access — they can already see
    the content)."""
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    client_authed_user.app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        ws.title = "Shared"
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id
    _seed_repo(tmp_path, wsid, "shared content")

    response = client_authed_user.get(f"/workspaces/{wsid}/export.md")
    assert response.status_code == 200
    assert response.text == "shared content"
