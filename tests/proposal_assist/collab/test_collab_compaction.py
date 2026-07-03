
from pathlib import Path

from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.canonical import build_doc
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm, pm_to_markdown
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc, ydoc_to_pm_json
from tests.proposal_assist.collab.test_collab_checkpoint import _init_repo
from tests.proposal_assist.conftest import make_user, make_workspace


def _append(pg_session_factory, wid, md, actor_id=None):
    upd = pm_json_to_ydoc(markdown_to_pm(md)).get_update()
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=upd)
        s.commit()


def _mk_ws(pg_session_factory, email):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email=email))
        s.commit()
        return ws.id


def test_compact_snapshot_reuses_min_id_and_preserves_doc(pg_session_factory):
    wid = _mk_ws(pg_session_factory, "compact-min@ki.se")
    _append(pg_session_factory, wid, "## A\n\nBody a.\n")
    _append(pg_session_factory, wid, "## A\n\nBody a.\n\n## B\n\nBody b.\n")
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    assert len(rows) == 2
    before = ydoc_to_pm_json(build_doc([b for (_, _, b) in rows]))
    min_id = rows[0][0]
    merged = build_doc([b for (_, _, b) in rows]).get_update()
    with pg_session_factory() as s:
        store.compact_snapshot(
            s, workspace_id=wid, artifact="draft", min_id=min_id,
            merged_update=merged, merged_actor_id=None, delete_ids=[rows[1][0]],
        )
        s.commit()
    with pg_session_factory() as s:
        after_rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        after_updates = store.load_updates(s, workspace_id=wid, artifact="draft")
    assert [r[0] for r in after_rows] == [min_id]          # only the reused row survives
    assert ydoc_to_pm_json(build_doc(after_updates)) == before   # doc unchanged


def test_compact_snapshot_merged_replays_before_a_lower_tail(pg_session_factory):
    # THE ordering regression (Codex High): a tail row can end up with an id LOWER
    # than a naive new merged row; the id-reuse design keeps the merged row at
    # min_id so it ALWAYS replays first. Simulate a tail that committed during the
    # checkpoint window (id > max_id) and assert convergence.
    wid = _mk_ws(pg_session_factory, "compact-order@ki.se")
    _append(pg_session_factory, wid, "## A\n\nBody a.\n")                     # id 1
    _append(pg_session_factory, wid, "## A\n\nBody a.\n\n## B\n\nBody b.\n")  # id 2 (snapshot max)
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    min_id, max_id = rows[0][0], rows[-1][0]
    merged = build_doc([b for (_, _, b) in rows]).get_update()
    # A concurrent client append lands AFTER the snapshot (id > max_id) = tail.
    _append(pg_session_factory, wid, "## A\n\nBody a EDITED.\n\n## B\n\nBody b.\n")
    with pg_session_factory() as s:
        store.compact_snapshot(
            s, workspace_id=wid, artifact="draft", min_id=min_id,
            merged_update=merged, merged_actor_id=None, delete_ids=[max_id],
        )
        s.commit()
    with pg_session_factory() as s:
        after = store.load_updates(s, workspace_id=wid, artifact="draft")
        after_rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    # merged row kept min_id, tail kept its higher id → ORDER BY id = [merged, tail].
    assert after_rows[0][0] == min_id and after_rows[-1][0] > max_id
    # Replays without loss (merged→tail converges; the bug shape [tail, merged] would not).
    assert "EDITED." in pm_to_markdown(ydoc_to_pm_json(build_doc(after)))


def test_compact_snapshot_does_not_clobber_unlisted_rows(pg_session_factory):
    wid = _mk_ws(pg_session_factory, "compact-noclobber@ki.se")
    _append(pg_session_factory, wid, "## A\n\nBody a.\n")
    _append(pg_session_factory, wid, "## A\n\nBody a2.\n")
    _append(pg_session_factory, wid, "## A\n\nBody a3.\n")
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    # Compact only the first two; the third must survive (delete by explicit id).
    merged = build_doc([rows[0][2], rows[1][2]]).get_update()
    with pg_session_factory() as s:
        store.compact_snapshot(
            s, workspace_id=wid, artifact="draft", min_id=rows[0][0],
            merged_update=merged, merged_actor_id=None, delete_ids=[rows[1][0]],
        )
        s.commit()
    with pg_session_factory() as s:
        after = [r[0] for r in store.load_rows(s, workspace_id=wid, artifact="draft")]
    assert after == [rows[0][0], rows[2][0]]   # min_id (reused) + the untouched third


def _enable(pg_session_factory, wid):
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()


def test_checkpoint_compacts_log_to_single_row(pg_session_factory, tmp_path):
    wid = _mk_ws(pg_session_factory, "cp-compact@ki.se")
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## A\n\nseed.\n")
    _enable(pg_session_factory, wid)
    # Two AUTHORED edits (actor_id set → checkpoint commits + compacts).
    _append(pg_session_factory, wid, "## A\n\nedit one.\n", actor_id=1)
    _append(pg_session_factory, wid, "## A\n\nedit one.\n\n## B\n\nedit two.\n", actor_id=1)
    with pg_session_factory() as s:
        before = ydoc_to_pm_json(
            build_doc(store.load_updates(s, workspace_id=wid, artifact="draft"))
        )
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        after = ydoc_to_pm_json(
            build_doc(store.load_updates(s, workspace_id=wid, artifact="draft"))
        )
    assert len(rows) == 1                # collapsed to the single merged row
    assert after == before               # doc unchanged by compaction


def test_checkpoint_single_row_no_compaction_noop(pg_session_factory, tmp_path):
    wid = _mk_ws(pg_session_factory, "cp-single@ki.se")
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## A\n\nseed.\n")
    _enable(pg_session_factory, wid)
    _append(pg_session_factory, wid, "## A\n\none edit.\n", actor_id=1)
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    assert len(rows) == 1   # a single-row log is left as-is (no pointless rewrite)


def test_compact_snapshot_preserves_merged_actor_id(pg_session_factory):
    wid = _mk_ws(pg_session_factory, "compact-actor@ki.se")
    _append(pg_session_factory, wid, "## A\n\nseed.\n")                  # unauthored seed
    _append(pg_session_factory, wid, "## A\n\nauthored.\n", actor_id=1)  # authored tail
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    merged = build_doc([b for (_, _, b) in rows]).get_update()
    with pg_session_factory() as s:
        store.compact_snapshot(
            s, workspace_id=wid, artifact="draft", min_id=rows[0][0],
            merged_update=merged, merged_actor_id=1, delete_ids=[rows[1][0]],
        )
        s.commit()
    with pg_session_factory() as s:
        after = store.load_rows(s, workspace_id=wid, artifact="draft")
    assert [r[0] for r in after] == [rows[0][0]]  # collapsed to the reused min_id row
    # merged row carries the last author, not the seed's None
    assert after[0][1] == 1


def test_checkpoint_compaction_preserves_last_author(pg_session_factory, tmp_path):
    wid = _mk_ws(pg_session_factory, "cp-actor@ki.se")
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## A\n\nseed.\n")
    _enable(pg_session_factory, wid)
    # unauthored seed → becomes min_id row
    _append(pg_session_factory, wid, "## A\n\nseed.\n")
    # authored tail
    _append(pg_session_factory, wid, "## A\n\nauthored edit.\n", actor_id=1)
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    assert len(rows) == 1
    # last author preserved through compaction even though min_id row was an unauthored seed
    assert rows[-1][1] == 1
