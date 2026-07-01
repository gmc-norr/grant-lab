# tests/proposal_assist/collab/test_collab_hydrate.py
import subprocess
from pathlib import Path

from pycrdt import Doc

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from tests.proposal_assist.collab.test_collab_recovery import _canonical
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def test_hydrates_from_git_when_store_empty_and_enabled(pg_session_factory, tmp_path):
    md = "preamble\n\n## Background\nhello **world**.\n"
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-o@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", md)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
        assert len(updates) == 1  # a single seed update
    # the seed was persisted (so the store is now canonical)
    with pg_session_factory() as s:
        assert len(store.load_updates(s, workspace_id=wid, artifact="draft")) == 1
        # and the hydrated doc reconstructs the git markdown's PM JSON (modulo the
        # known attrs:{} representation difference the recovery test's _canonical drops)
        doc = build_doc(updates)
        assert _canonical(ydoc_to_pm_json(doc)) == _canonical(markdown_to_pm(md))


def test_no_hydrate_when_flag_off(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-off@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## X\nbody\n")
    with pg_session_factory() as s:
        assert (
            load_or_hydrate(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
            == []
        )
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == []  # nothing persisted


def test_existing_updates_are_replayed_not_rehydrated(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-ex@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## X\nGIT.\n")
    # pre-existing live edits in the store (different from git):
    d = Doc()
    from pycrdt import Text

    d["body"] = Text()
    cap: list[bytes] = []
    sub = d.observe(lambda e: cap.append(e.update))
    d["body"] += "LIVE"
    d.unobserve(sub)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=cap[-1])
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        assert updates == cap  # replayed the store, did NOT re-hydrate from git
