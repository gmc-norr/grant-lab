"""3b-5d.D4 severance: CM6 saves 409 on a collab-enabled artifact (a stale tab
must not git-first-write around the live room); kill-switch restores git-first."""

import subprocess

import pytest

from call_manager.proposal_assist.collab import state
from tests.proposal_assist.conftest import make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nA plain claim.\n"
SAVE_BODY_TEMPLATE = {"content": "## Body\nrewritten.\n"}


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def _seed(s, tmp_path, owner, *, enable=(), with_spec=False):
    ws = make_workspace(s, owner=owner)
    repo = _init_repo(tmp_path, ws.id, DOC)
    if with_spec:
        (repo / "proposal-spec.md").write_text("## Approach\nspec body.\n")
        subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "spec"], cwd=repo, check=True)
    for artifact in enable:
        state.enable_collab(s, workspace_id=ws.id, artifact=artifact)
    return ws, repo


def _save_payload(repo, fname="proposal.md", index=1, content="## Body\nrewritten.\n"):
    from call_manager.proposal_assist.citations.gitio import current_head
    from call_manager.proposal_assist.editing.sectionizer import split_sections

    doc = (repo / fname).read_text()
    sec = split_sections(doc)[index]
    return {
        "content": content,
        "base_commit_sha": current_head(repo),
        "base_section_sha": sec.sha,
    }


def test_draft_save_409_when_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("draft",))
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 409
    assert "live editor" in r.json()["error"]["message"]
    assert (repo / "proposal.md").read_text() == DOC  # nothing written


def test_spec_save_409_when_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("spec",), with_spec=True)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/spec/sections/1",
        json=_save_payload(repo, fname="proposal-spec.md", content="## Approach\nnew.\n"),
    )
    assert r.status_code == 409


def test_draft_save_works_when_only_spec_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """Per-artifact: an enabled SPEC must not sever DRAFT saves."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("spec",), with_spec=True)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 200


def test_kill_switch_restores_git_first_save(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """With the switch on, the mask means NO 409 — the CM6 world is back."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("draft",))
        s.commit()
        wid = ws.id
    state.set_kill_switch(True)
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 200
    assert "rewritten." in (repo / "proposal.md").read_text()
