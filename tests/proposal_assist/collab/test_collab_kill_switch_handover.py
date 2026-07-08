"""3b-5d.D4 §3.2: the drain-then-truncate handover makes git the SOLE canonical
store while the kill-switch is engaged — closing BOTH clobber directions."""

import subprocess
from pathlib import Path

from pycrdt import Doc

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.citations.gitio import current_head, write_and_commit_files
from call_manager.proposal_assist.collab import handover, state, store
from call_manager.proposal_assist.collab.checkpoint import serialize_doc_to_markdown
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.proposal_assist.conftest import make_user, make_workspace


def _init_repo(repo: Path, content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _seed_room_edit(pg_session_factory, wid, actor_id, md, *, artifact="draft") -> Doc:
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact=artifact, actor_id=actor_id, update=doc.get_update()
        )
        s.commit()
    return doc


def _setup(pg_session_factory, tmp_path, email):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, "## Background\noriginal.\n")
    return wid, oid, repo


def _init_repo_with_spec(repo: Path, draft_content: str, spec_content: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(draft_content)
    (repo / SPEC_FILENAME).write_text(spec_content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _setup_spec(pg_session_factory, tmp_path, email):
    """Spec twin of _setup: the repo carries BOTH artifacts' files (like a real
    workspace does) so a spec-only drain's cross-artifact isolation is a real
    assertion, not vacuously true."""
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o)
        state.enable_collab(s, workspace_id=ws.id, artifact="spec")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo_with_spec(repo, "## Background\noriginal draft.\n", "## Approach\noriginal spec.\n")
    return wid, oid, repo


def _log_len(s, wid, *, artifact="draft"):
    return len(store.load_rows(s, workspace_id=wid, artifact=artifact))


def test_enter_direction_small_tail_drained_and_truncated(pg_session_factory, tmp_path):
    """A 1-row tail (far below the 200 scan floor) is checkpointed to git and
    the log is truncated — a subsequent git-first save can never be clobbered."""
    wid, oid, repo = _setup(pg_session_factory, tmp_path, "ho1@ki.se")
    doc = _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\nlive tail edit.\n")

    n = handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path))
    assert n == 1
    assert (repo / PROPOSAL_FILENAME).read_text() == serialize_doc_to_markdown(doc)
    with pg_session_factory() as s:
        assert _log_len(s, wid) == 0
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0

    # kill-window git-first save lands; a second pass has nothing to clobber with
    write_and_commit_files(repo, {PROPOSAL_FILENAME: "## Background\nkill-window save.\n"}, "cm6")
    head = current_head(repo)
    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 0
    assert current_head(repo) == head  # untouched


def test_fully_checkpointed_log_is_still_truncated(pg_session_factory, tmp_path):
    """Exit-direction guard: even with NO uncheckpointed tail, the stale log must
    go — otherwise post-switch replay resurrects pre-kill content over
    kill-window git edits."""
    from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact

    wid, oid, repo = _setup(pg_session_factory, tmp_path, "ho2@ki.se")
    _seed_room_edit(pg_session_factory, wid, oid, "## Background\n\ncheckpointed edit.\n")
    with pg_session_factory() as s:
        checkpoint_artifact(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
    with pg_session_factory() as s:
        assert _log_len(s, wid) >= 1  # log present, tail == 0

    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 1
    with pg_session_factory() as s:
        assert _log_len(s, wid) == 0

    # exit direction: kill-window git edit, then a fresh room open hydrates from CURRENT HEAD
    write_and_commit_files(repo, {PROPOSAL_FILENAME: "## Background\nkill-window truth.\n"}, "cm6")
    with pg_session_factory() as s:
        load_or_hydrate(s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path))
        s.commit()
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
    assert len(rows) == 1  # fresh hydration seed
    from call_manager.proposal_assist.collab.hydrate import build_doc

    assert "kill-window truth." in serialize_doc_to_markdown(build_doc([rows[0][2]]))


def test_failure_is_isolated_and_retryable(pg_session_factory, tmp_path, monkeypatch):
    """A truncate failure on one artifact rolls back atomically (checkpoint+truncate
    are one txn — the log survives, retry succeeds) and does not block others."""
    wid_a, oid_a, _ = _setup(pg_session_factory, tmp_path, "ho3a@ki.se")
    wid_b, oid_b, _ = _setup(pg_session_factory, tmp_path, "ho3b@ki.se")
    _seed_room_edit(pg_session_factory, wid_a, oid_a, "## Background\n\nA edit.\n")
    _seed_room_edit(pg_session_factory, wid_b, oid_b, "## Background\n\nB edit.\n")

    real_truncate = store.truncate_log

    def _fail_for_a(session, *, workspace_id, artifact):
        if workspace_id == wid_a:
            raise RuntimeError("boom")
        return real_truncate(session, workspace_id=workspace_id, artifact=artifact)

    monkeypatch.setattr(handover.store, "truncate_log", _fail_for_a)
    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 1
    with pg_session_factory() as s:
        assert _log_len(s, wid_a) >= 1  # rolled back atomically — log intact
        assert _log_len(s, wid_b) == 0  # B unaffected

    monkeypatch.setattr(handover.store, "truncate_log", real_truncate)
    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 1


def test_repoless_log_is_never_truncated(pg_session_factory, tmp_path):
    """No git repo = nowhere to hand the content over to — the log must survive
    (data preservation beats the pass completing)."""
    with pg_session_factory() as s:
        o = make_user(s, email="ho4@ki.se")
        ws = make_workspace(s, owner=o)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, o.id
    _seed_room_edit(pg_session_factory, wid, oid, "## X\n\norphan edit.\n")  # NO repo on disk

    assert handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path)) == 0
    with pg_session_factory() as s:
        assert _log_len(s, wid) == 1


def test_handover_drains_and_truncates_spec_artifact(pg_session_factory, tmp_path):
    """Spec twin of test_enter_direction_small_tail_drained_and_truncated: the
    drain-then-truncate handover is per-(workspace, artifact) — Artifact.SPEC
    checkpoints to proposal-spec.md via filename_for, not proposal.md. A
    spec-only uncheckpointed tail is drained and truncated exactly like the
    draft path, while the co-located draft artifact in the SAME workspace/repo
    is left completely untouched (cross-artifact isolation — the checkpoint
    write must never clobber the sibling artifact's file or log)."""
    wid, oid, repo = _setup_spec(pg_session_factory, tmp_path, "ho5@ki.se")
    doc = _seed_room_edit(
        pg_session_factory, wid, oid, "## Approach\n\nlive spec edit.\n", artifact="spec"
    )
    draft_before = (repo / PROPOSAL_FILENAME).read_text()

    n = handover.run_kill_switch_handover(pg_session_factory, workspaces_dir=str(tmp_path))
    assert n == 1
    assert (repo / SPEC_FILENAME).read_text() == serialize_doc_to_markdown(doc)
    with pg_session_factory() as s:
        assert _log_len(s, wid, artifact="spec") == 0
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="spec") == 0
    # cross-artifact isolation: the draft file/log are untouched by the spec-only drain
    assert (repo / PROPOSAL_FILENAME).read_text() == draft_before
    with pg_session_factory() as s:
        assert _log_len(s, wid, artifact="draft") == 0
