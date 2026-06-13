"""Tests for the 4 artifact endpoints: raw / history / at / diff."""

import subprocess
from pathlib import Path

from tests.proposal_assist.conftest import make_workspace


def _init_workspace_repo(workspaces_dir: Path, wsid) -> list[str]:
    """Create a 3-commit repo for the workspace. Return shas."""
    repo_dir = workspaces_dir / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo_dir, check=True)
    shas = []
    for i in range(3):
        (repo_dir / "proposal.md").write_text(f"# V{i}\n\nv{i}.")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"commit {i}"], cwd=repo_dir, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        shas.append(sha)
    return shas


def _seed(pg_session_factory, current_user, client, tmp_path):
    client.app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id
    shas = _init_workspace_repo(tmp_path, wsid)
    return wsid, shas


def test_artifact_raw_returns_current_content(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    wsid, _ = _seed(pg_session_factory, current_user, client_authed_owner, tmp_path)
    response = client_authed_owner.get(f"/workspaces/{wsid}/artifact/raw")
    assert response.status_code == 200
    assert "v2" in response.text  # latest commit's content


def test_artifact_history_returns_commits_list(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    wsid, shas = _seed(pg_session_factory, current_user, client_authed_owner, tmp_path)
    response = client_authed_owner.get(f"/workspaces/{wsid}/artifact/history")
    assert response.status_code == 200
    for sha in shas:
        # Short shas appear (first 8 chars per the template).
        assert sha[:8].encode() in response.content


def test_artifact_at_returns_old_content(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    wsid, shas = _seed(pg_session_factory, current_user, client_authed_owner, tmp_path)
    response = client_authed_owner.get(f"/workspaces/{wsid}/artifact/at/{shas[0]}")
    assert response.status_code == 200
    assert "v0" in response.text


def test_artifact_diff_returns_unified_diff(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    wsid, shas = _seed(pg_session_factory, current_user, client_authed_owner, tmp_path)
    response = client_authed_owner.get(
        f"/workspaces/{wsid}/artifact/diff?commit_a={shas[0]}&commit_b={shas[-1]}",
    )
    assert response.status_code == 200
    assert "v0" in response.text and "v2" in response.text


def test_artifact_at_rejects_invalid_sha(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """B5's sha validation should propagate as a 400."""
    wsid, _ = _seed(pg_session_factory, current_user, client_authed_owner, tmp_path)
    response = client_authed_owner.get(
        f"/workspaces/{wsid}/artifact/at/--output=evil",
    )
    # Either 400 (we mapped the error explicitly) or 422 (FastAPI's path
    # validator caught the malformed sha). Both prevent the attack.
    assert response.status_code in (400, 422, 404)


def test_artifact_diff_rejects_invalid_sha(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """B5 / Codex security regression: --output=proposal.md must be
    rejected before reaching git."""
    wsid, shas = _seed(pg_session_factory, current_user, client_authed_owner, tmp_path)
    response = client_authed_owner.get(
        f"/workspaces/{wsid}/artifact/diff?commit_a=--output=proposal.md&commit_b={shas[-1]}",
    )
    assert response.status_code in (400, 422)


def test_artifact_endpoints_allow_viewer(
    client_authed_user,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """Viewers (not owners) can also read artifact endpoints."""
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    client_authed_user.app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id
    _init_workspace_repo(tmp_path, wsid)
    response = client_authed_user.get(f"/workspaces/{wsid}/artifact/raw")
    assert response.status_code == 200


def test_artifact_route_returns_body_without_nested_wrapper(
    client_authed_owner, pg_session_factory, current_user
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    resp = client_authed_owner.get(f"/workspaces/{wsid}/artifact")
    assert resp.status_code == 200
    body = resp.content.decode()
    # No nested #artifact-rendered (the route returns only the body).
    assert 'id="artifact-rendered"' not in body


def test_artifact_route_empty_returns_placeholder_not_blank(
    client_authed_owner, pg_session_factory, current_user
):
    """A workspace with no proposal.md yet returns the placeholder, NOT a
    blank body (so a failed first turn doesn't blank the pane)."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    resp = client_authed_owner.get(f"/workspaces/{wsid}/artifact")
    assert resp.status_code == 200
    assert "no proposal.md yet" in resp.content.decode()


def test_artifact_route_404_missing_workspace(client_authed_owner):
    import uuid

    resp = client_authed_owner.get(f"/workspaces/{uuid.uuid4()}/artifact")
    assert resp.status_code == 404


def test_artifact_route_allows_viewer(
    client_authed_user, pg_session_factory, current_user
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="owner3@ki.se")
        ws = make_workspace(s, owner=owner)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id

    resp = client_authed_user.get(f"/workspaces/{wsid}/artifact")
    assert resp.status_code == 200
