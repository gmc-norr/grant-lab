"""Section-editing web routes (HTMX fragments) — Task 7.

Fixture pattern mirrors tests/proposal_assist/conftest.py
(create_app + signed session cookie), with three role clients
(owner / viewer / outsider) sharing ONE app instance. Unlike the
conftest clients, these do NOT enter the TestClient context manager:
running the lifespan three times for one app would triple-start the
scheduler + admin bootstrap, and none of the section routes depend on
lifespan-created state (settings / session_factory / templates /
session_signer are all set in create_app()).

Repo layout is the PRODUCTION one: <workspaces_dir>/<workspace_id>/repo
(reuses _init_repo from tests/test_editing_save.py).
"""

import re

import pytest
from fastapi.testclient import TestClient

from call_manager.db.models import TurnJob, User, Workspace, WorkspaceViewer
from call_manager.main import create_app
from tests.test_editing_save import _init_repo


@pytest.fixture
def _settings_env(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


@pytest.fixture
def app(_settings_env, pg_session_factory, tmp_path):
    a = create_app()
    # Point the app at the test DB + an isolated workspaces root. Settings
    # is cached on app.state at create_app() time, so mutate the instance
    # (same pattern as test_web_workspaces_main.py).
    a.state.session_factory = pg_session_factory
    a.state.settings.workspaces_dir = str(tmp_path)
    return a


@pytest.fixture
def users(app, pg_session_factory):
    """owner / viewer / outsider User ids."""
    with pg_session_factory() as s:
        rows = {role: User(email=f"sec-{role}@ki.se") for role in ("owner", "viewer", "outsider")}
        s.add_all(rows.values())
        s.commit()
        return {role: u.id for role, u in rows.items()}


def _client_for(app, user_id) -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return client


@pytest.fixture
def client_owner(app, users):
    return _client_for(app, users["owner"])


@pytest.fixture
def client_viewer(app, users):
    return _client_for(app, users["viewer"])


@pytest.fixture
def client_outsider(app, users):
    return _client_for(app, users["outsider"])


@pytest.fixture
def ws_with_repo(app, users, pg_session_factory, tmp_path):
    """Workspace owned by `owner`, with `viewer` granted read access, and a
    git repo at the production layout <workspaces_dir>/<id>/repo."""
    with pg_session_factory() as s:
        ws = Workspace(owner_id=users["owner"], title="sections-ws")
        s.add(ws)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=users["viewer"]))
        s.commit()
        wsid = ws.id
    _init_repo(tmp_path, wsid)
    return wsid


def _extract_hidden(html: str, name: str) -> str:
    m = re.search(rf'name="{name}" value="([0-9a-f]+)"', html)
    assert m, f"hidden field {name} not found in fragment:\n{html}"
    return m.group(1)


def test_edit_fragment_contains_raw_and_base_sha(client_owner, ws_with_repo):
    r = client_owner.get(f"/workspaces/{ws_with_repo}/sections/1/edit")
    assert r.status_code == 200
    assert "<textarea" in r.text
    assert "base_commit_sha" in r.text
    assert "base_section_sha" in r.text
    # The raw section bytes are in the textarea (escaped or not).
    assert "old body" in r.text


def test_save_roundtrip_updates_view(client_owner, ws_with_repo, tmp_path):
    edit = client_owner.get(f"/workspaces/{ws_with_repo}/sections/1/edit")
    assert edit.status_code == 200
    base_commit_sha = _extract_hidden(edit.text, "base_commit_sha")
    base_section_sha = _extract_hidden(edit.text, "base_section_sha")

    r = client_owner.post(
        f"/workspaces/{ws_with_repo}/sections/1",
        data={
            "content": "## Background\nnew body\n",
            "base_commit_sha": base_commit_sha,
            "base_section_sha": base_section_sha,
        },
    )
    assert r.status_code == 200
    assert 'id="section-1"' in r.text
    assert "new body" in r.text
    proposal = tmp_path / str(ws_with_repo) / "repo" / "proposal.md"
    assert proposal.read_text() == "# T\nintro\n## Background\nnew body\n## Aims\naims\n"


def test_viewer_cannot_edit(client_viewer, ws_with_repo):
    r = client_viewer.get(f"/workspaces/{ws_with_repo}/sections/1/edit")
    assert r.status_code == 403
    r = client_viewer.post(
        f"/workspaces/{ws_with_repo}/sections/1",
        data={
            "content": "## Background\nhax\n",
            "base_commit_sha": "0" * 40,
            "base_section_sha": "0" * 64,
        },
    )
    assert r.status_code == 403


def test_viewer_can_view_section(client_viewer, ws_with_repo):
    r = client_viewer.get(f"/workspaces/{ws_with_repo}/sections/1")
    assert r.status_code == 200
    assert 'id="section-1"' in r.text
    # No Edit button for read-only viewers.
    assert "section-edit-btn" not in r.text


def test_outsider_cannot_read_sections(client_outsider, ws_with_repo):
    """IDOR regression: an authenticated but unrelated user gets 403."""
    r = client_outsider.get(f"/workspaces/{ws_with_repo}/sections/1")
    assert r.status_code == 403


def test_save_during_turn_409(client_owner, ws_with_repo, pg_session_factory):
    edit = client_owner.get(f"/workspaces/{ws_with_repo}/sections/1/edit")
    base_commit_sha = _extract_hidden(edit.text, "base_commit_sha")
    base_section_sha = _extract_hidden(edit.text, "base_section_sha")
    with pg_session_factory() as s:
        s.add(TurnJob(workspace_id=ws_with_repo, status="queued"))
        s.commit()
    r = client_owner.post(
        f"/workspaces/{ws_with_repo}/sections/1",
        data={
            "content": "## Background\nracing the agent\n",
            "base_commit_sha": base_commit_sha,
            "base_section_sha": base_section_sha,
        },
    )
    assert r.status_code == 409
    # The edit fragment also refuses while the turn is in flight.
    r = client_owner.get(f"/workspaces/{ws_with_repo}/sections/1/edit")
    assert r.status_code == 409


def test_stale_save_409(client_owner, ws_with_repo):
    r = client_owner.post(
        f"/workspaces/{ws_with_repo}/sections/1",
        data={
            "content": "## Background\nstale\n",
            "base_commit_sha": "f" * 40,
            "base_section_sha": "f" * 64,
        },
    )
    assert r.status_code == 409


def test_heading_removal_400(client_owner, ws_with_repo):
    edit = client_owner.get(f"/workspaces/{ws_with_repo}/sections/1/edit")
    base_commit_sha = _extract_hidden(edit.text, "base_commit_sha")
    base_section_sha = _extract_hidden(edit.text, "base_section_sha")
    r = client_owner.post(
        f"/workspaces/{ws_with_repo}/sections/1",
        data={
            "content": "no heading anymore\n",
            "base_commit_sha": base_commit_sha,
            "base_section_sha": base_section_sha,
        },
    )
    assert r.status_code == 400


def test_workspace_main_renders_sections(client_owner, ws_with_repo):
    r = client_owner.get(f"/workspaces/{ws_with_repo}")
    assert r.status_code == 200
    assert 'id="section-1"' in r.text
    assert "section-edit-btn" in r.text
