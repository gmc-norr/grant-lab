# tests/proposal_assist/collab/test_collab_hydrate.py
import subprocess
from pathlib import Path

from pycrdt import Doc, Text

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import serialize_doc_to_markdown
from call_manager.proposal_assist.collab.hydrate import _EMPTY_DOC, build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.collab.test_collab_recovery import _canonical
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for


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
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
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
            load_or_hydrate(
                s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
                workspaces_dir=str(tmp_path),
            )
            == []
        )
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == []  # nothing persisted


def test_hydrates_empty_paragraph_when_head_is_blank(pg_session_factory, tmp_path):
    # A whitespace-only HEAD has no valid ProseMirror representation (a `doc`
    # node needs `block+`), so hydration must seed the canonical empty-paragraph
    # doc rather than crash on markdown_to_pm(""). (Finding D.)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-blank@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "   \n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
        assert len(updates) == 1  # seeded, did not crash
    with pg_session_factory() as s:
        assert len(store.load_updates(s, workspace_id=wid, artifact="draft")) == 1
    doc = build_doc(updates)
    assert _canonical(ydoc_to_pm_json(doc)) == _canonical(_EMPTY_DOC)
    # the empty seed is checkpoint-safe: it serializes and preserves the
    # split_sections join invariant.
    md = serialize_doc_to_markdown(doc)
    assert "".join(sec.text for sec in split_sections(md)) == md


def test_no_hydrate_when_enabled_but_artifact_file_missing(pg_session_factory, tmp_path):
    # Collab enabled but the artifact was never committed (no git HEAD to seed
    # from): stay Slice-1 empty rather than persist a synthetic seed. (Finding D.)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-missing@ki.se"))
        s.commit()
        wid = ws.id
    # deliberately do NOT init a repo / write proposal.md
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        assert (
            load_or_hydrate(
                s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
                workspaces_dir=str(tmp_path),
            )
            == []
        )
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == []


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
            s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        assert updates == cap  # replayed the store, did NOT re-hydrate from git


# --- The epoch rides with the rows (spec 2026-09-02-collab-doc-epoch-design.md §3.2) ---


def test_warm_path_returns_a_hydration_carrying_the_rows_epoch(pg_session_factory, tmp_path):
    from call_manager.proposal_assist.collab.hydrate import Hydration

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-ep-warm@ki.se"))
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        # Two truncates happened before this room open: the rows that exist
        # now belong to document 2.
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        d = Doc()
        d["body"] = Text()
        d["body"] += "warm"
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=d.get_update()
        )
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
    assert isinstance(updates, Hydration)
    assert isinstance(updates, list)  # still a list to every existing caller
    assert len(updates) == 1
    assert updates.doc_epoch == 2


def test_cold_seed_path_carries_the_epoch_too(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-ep-cold@ki.se"))
        s.commit()
        wid = ws.id
    _init_repo(Path(tmp_path) / str(wid) / "repo", "## Background\nseeded.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")  # epoch 1, log empty
        s.commit()
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
        s.commit()
    assert len(updates) == 1  # the seed
    assert updates.doc_epoch == 1


def test_flag_off_and_never_reset_is_epoch_zero(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="hy-ep-off@ki.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s,
            stamp=stamp_for(s, wid),
            workspace_id=wid,
            artifact="draft",
            workspaces_dir=str(tmp_path),
        )
    assert updates == []
    assert updates.doc_epoch == 0
