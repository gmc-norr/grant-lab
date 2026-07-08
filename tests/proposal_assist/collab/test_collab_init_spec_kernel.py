"""3b-5d.D4: 'Start spec from skeleton' goes through the KERNEL when the spec is
collab-enabled — an open empty room gets the skeleton live; git-side divergence
is impossible."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME, SPEC_SKELETON
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown
from tests.proposal_assist.conftest import make_workspace


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def _init_repo_with_blank_spec(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text("# T\nintro\n## Body\nx.\n")
    (repo / SPEC_FILENAME).write_text("\n")  # whitespace-only -> seedable
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _seed(s, tmp_path, owner, *, enable_spec=True):
    ws = make_workspace(s, owner=owner)
    _init_repo_with_blank_spec(Path(tmp_path) / str(ws.id) / "repo")
    if enable_spec:
        state.enable_collab(s, workspace_id=ws.id, artifact="spec")
    return ws


def test_enabled_init_seeds_skeleton_via_room(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = _seed(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{wid}/spec")
    assert r.status_code == 200
    body = r.json()
    joined = "".join(sec["text"] for sec in body["sections"])
    assert "## " in joined  # skeleton headings present
    with pg_session_factory() as s:
        # the ROOM is canonical and holds the skeleton (log non-empty)
        assert len(store.load_rows(s, workspace_id=wid, artifact="spec")) >= 1
        canon = canonical_artifact_markdown(
            s, workspace_id=wid, artifact="spec",
            repo_dir=Path(tmp_path) / str(wid) / "repo",
        )
    assert canon == joined  # response sections == room serialization
    # git received the checkpoint commit too
    disk = (Path(tmp_path) / str(wid) / "repo" / SPEC_FILENAME).read_text()
    assert disk == canon


def test_enabled_init_is_noop_on_nonempty_room(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = _seed(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    assert client_authed_owner.post(f"/api/workspaces/{wid}/spec").status_code == 200
    with pg_session_factory() as s:
        n_rows = len(store.load_rows(s, workspace_id=wid, artifact="spec"))
    r2 = client_authed_owner.post(f"/api/workspaces/{wid}/spec")  # idempotent repeat
    assert r2.status_code == 200
    with pg_session_factory() as s:
        assert len(store.load_rows(s, workspace_id=wid, artifact="spec")) == n_rows


def test_enabled_init_409_on_locked_spec(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = _seed(s, tmp_path, current_user)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = current_user.id
        s.commit()
        wid = ws.id
    assert client_authed_owner.post(f"/api/workspaces/{wid}/spec").status_code == 409


def test_disabled_init_takes_git_path_unchanged(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = _seed(s, tmp_path, current_user, enable_spec=False)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{wid}/spec")
    assert r.status_code == 200
    disk = (Path(tmp_path) / str(wid) / "repo" / SPEC_FILENAME).read_text()
    assert disk == SPEC_SKELETON  # byte-for-byte legacy write
    with pg_session_factory() as s:
        assert store.load_rows(s, workspace_id=wid, artifact="spec") == []  # room untouched
