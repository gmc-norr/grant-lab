"""Cockpit /spec JSON surface (GET/POST/init). Mirrors tests/test_api_workspaces.py:
same _app/_seed/_client helpers and production repo layout."""

from __future__ import annotations

import subprocess

from call_manager.proposal_assist.citations.gitio import _git_env
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.test_api_workspaces import _app, _client, _seed


def _add_spec(tmp_path, ws_id, content=""):
    """Write + commit proposal-spec.md into the already-initialized repo."""
    repo = workspace_repo_dir(str(tmp_path), ws_id)
    (repo / "proposal-spec.md").write_text(content)
    env = _git_env()
    subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed spec"], cwd=repo, check=True, env=env)


def test_get_spec_returns_sections_no_citations(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft preamble\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    r = c.get(f"/api/workspaces/{ws_id}/spec")
    assert r.status_code == 200
    body = r.json()
    assert "citations" not in body  # SpecOut has no citations field
    assert body["can_edit"] is True
    assert [s["index"] for s in body["sections"]] == [0, 1]  # empty preamble + Aims
    assert body["sections"][1]["heading"] == "Aims"


def test_get_spec_member_allowed_stranger_forbidden(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    assert _client(app, member_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 200
    assert _client(app, stranger_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 403


def test_get_spec_404_when_file_absent(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    # No _add_spec -> proposal-spec.md does not exist.
    assert _client(app, owner_id).get(f"/api/workspaces/{ws_id}/spec").status_code == 404
