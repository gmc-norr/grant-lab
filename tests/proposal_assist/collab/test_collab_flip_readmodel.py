"""3b-5d.D4: the first artifact read flips collab on (enable-on-touch)."""

import subprocess

import pytest

from grant_lab.db.models import CollabArtifactState
from grant_lab.proposal_assist.collab import state, store
from grant_lab.proposal_assist.collab.checkpoint import serialize_doc_to_markdown
from grant_lab.proposal_assist.collab.markdown_bridge import markdown_to_pm
from grant_lab.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.proposal_assist.conftest import make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nA plain claim.\n"
TABLE_DOC = "# T\nintro\n## Body\n| a | b |\n|---|---|\n"


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def _seed(s, tmp_path, owner, doc=DOC, *, with_spec=False):
    ws = make_workspace(s, owner=owner)
    repo = _init_repo(tmp_path, ws.id, doc)
    if with_spec:
        (repo / "proposal-spec.md").write_text("## Approach\nspec body.\n")
        subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "spec"], cwd=repo, check=True)
    return ws, repo


def _flag_row(s, wid, artifact):
    return s.get(CollabArtifactState, (wid, artifact))


def test_first_draft_read_flips_and_reports_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/draft")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is True  # SAME response reflects the flip
    with pg_session_factory() as s:
        row = _flag_row(s, wid, "draft")
        assert row is not None and row.enabled_at is not None
        assert _flag_row(s, wid, "spec") is None  # per-artifact: spec untouched


def test_spec_read_flips_spec_independently(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user, with_spec=True)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/spec")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is True
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "spec").enabled_at is not None


def test_table_doc_enables_and_serves_canonical_table(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """Tables slice: pipe-table docs are no longer held back — they enable on
    first read and the response serves table content."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user, doc=TABLE_DOC)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/draft")
    assert r.status_code == 200
    body = r.json()
    assert body["collab_enabled"] is True
    joined = "".join(sec["text"] for sec in body["sections"])
    assert "| a | b |" in joined
    # space-padded canonical delimiter — unreachable by the old flattening path
    assert "| --- | --- |" in joined
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "draft").enabled_at is not None


def test_kill_switch_suppresses_flip(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    state.set_kill_switch(True)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    r = client_authed_owner.get(f"/api/workspaces/{wid}/draft")
    assert r.status_code == 200
    assert r.json()["collab_enabled"] is False
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "draft") is None


def test_repoless_workspace_reads_404_without_flipping(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)  # NO repo
        s.commit()
        wid = ws.id
    assert client_authed_owner.get(f"/api/workspaces/{wid}/draft").status_code == 404
    with pg_session_factory() as s:
        assert _flag_row(s, wid, "draft") is None


def test_get_spec_reads_canonical_room_content(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """Once enabled, spec sections derive from the LIVE room, not disk."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, _ = _seed(s, tmp_path, current_user, with_spec=True)
        s.commit()
        wid = ws.id
    assert client_authed_owner.get(f"/api/workspaces/{wid}/spec").status_code == 200  # flips
    doc = pm_json_to_ydoc(markdown_to_pm("## Approach\n\nlive room rewrite.\n"))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="spec", actor_id=current_user.id,
            update=doc.get_update(),
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{wid}/spec").json()
    joined = "".join(sec["text"] for sec in body["sections"])
    assert "live room rewrite." in joined          # canonical, not disk
    assert joined == serialize_doc_to_markdown(doc)  # exactly the room serialization
