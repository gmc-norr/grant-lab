"""Smoke for the workspace router skeleton. Subsequent tasks add real
behavior; here we just verify the router mounts + auth applies."""

import uuid

from tests.proposal_assist.conftest import make_workspace


def test_workspace_main_requires_auth(client_unauth, pg_session_factory):
    """Hitting GET /workspaces/<id> without a session redirects to login."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    response = client_unauth.get(
        f"/workspaces/{wsid}",
        follow_redirects=False,
        headers={"Accept": "text/html"},
    )
    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers.get("location", "")


def test_workspace_main_404_when_workspace_missing(client_authed_owner):
    """A non-existent workspace yields 404, not 500."""
    response = client_authed_owner.get(f"/workspaces/{uuid.uuid4()}")
    assert response.status_code == 404


def test_main_view_renders_chat_history(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
    monkeypatch,
):
    """chat_turns rows render as chat history in the left pane."""
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "hello"})
        s.commit()
        wsid = ws.id

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    assert b"hi" in response.content
    assert b"hello" in response.content


def test_main_view_renders_artifact_pane(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
    monkeypatch,
):
    """If proposal.md exists in the workspace repo, it's rendered."""
    import subprocess

    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    # Settings is cached on app.state at create_app() time, so monkeypatching
    # the env var does not take effect mid-test.  Directly override the
    # already-instantiated settings object instead.
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)

    repo_dir = tmp_path / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text("# Heading\n\nbody text.")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    # Rendered markdown produces <h1> for "# Heading".
    assert b"<h1>Heading</h1>" in response.content
    assert b"body text" in response.content
