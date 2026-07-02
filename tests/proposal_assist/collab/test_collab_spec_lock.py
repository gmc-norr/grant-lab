"""Collab-aware spec lock / unlock endpoint tests (Slice 2b-2, Task 5).

These cover the flag-ON paths only.  Flag-OFF parity is enforced by
tests/test_api_spec_lock.py which must stay green unchanged.

Patterns mirror test_collab_kernel_accept_route.py:
- `_app`, `pg_session_factory`, `tmp_path` fixtures from conftest / proposal_assist conftest.
- Repos initialised by _init_spec_repo (proposal.md + proposal-spec.md committed).
- Yjs updates seeded via store.append_update for artifact="spec".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pycrdt import Doc

from call_manager.db.models import SpecLockEvent
from call_manager.proposal_assist.artifacts import SPEC_FILENAME
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.test_api_spec import _add_spec
from tests.test_api_workspaces import _app, _client, _seed

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_spec_repo(tmp_path: Path, ws_id, *, spec_content: str) -> Path:
    """Initialise a workspace repo with both proposal.md and proposal-spec.md committed."""
    from call_manager.proposal_assist.paths import workspace_repo_dir

    repo = workspace_repo_dir(str(tmp_path), ws_id)
    # repo already exists from _seed; spec file may not — write and commit it.
    env = _git_env()
    (repo / SPEC_FILENAME).write_text(spec_content)
    subprocess.run(["git", "add", SPEC_FILENAME], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed spec"], cwd=repo, check=True, env=env)
    return repo


def _git_env():
    """Minimal git environment for test commits (avoids ~/.gitconfig lookup)."""
    from call_manager.proposal_assist.citations.gitio import _git_env as _real

    return _real()


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _seed_spec_room_update(pg_session_factory, ws_id, actor_id, md: str) -> Doc:
    """Build a Yjs doc from markdown and persist it as an update for artifact='spec'."""
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=ws_id, artifact="spec", actor_id=actor_id, update=doc.get_update()
        )
        s.commit()
    return doc


def _spec_lock_events(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return s.query(SpecLockEvent).filter(SpecLockEvent.workspace_id == ws_id).all()


# ---------------------------------------------------------------------------
# Test 1: collab lock stamps the FRESH checkpoint SHA
# ---------------------------------------------------------------------------


def test_collab_lock_stamps_fresh_checkpoint_sha(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Enable collab for spec; seed an uncheckpointed live-room spec edit;
    POST …/spec/lock → 200 locked; the SpecLockEvent.artifact_commit_sha equals
    the NEW checkpoint SHA (not the prior HEAD), and proposal-spec.md on disk
    matches the room content."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    repo = _init_spec_repo(tmp_path, ws_id, spec_content="## Aims\noriginal.\n")
    head_before = _git_head(repo)

    # Enable collab for spec artifact
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    # Seed an uncheckpointed live-room edit (actor = owner)
    edited_md = "## Aims\n\nedited by owner.\n"
    from call_manager.proposal_assist.collab.checkpoint import serialize_doc_to_markdown

    doc = _seed_spec_room_update(pg_session_factory, ws_id, owner_id, edited_md)
    expected_md = serialize_doc_to_markdown(doc)

    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locked"] is True

    # The SpecLockEvent SHA must be the FRESH checkpoint SHA (a new commit, not head_before)
    evs = _spec_lock_events(pg_session_factory, ws_id)
    assert len(evs) == 1
    assert evs[0].action == "lock"
    assert evs[0].artifact_commit_sha != head_before, (
        "Expected a new checkpoint SHA, not the pre-lock HEAD"
    )
    # Disk must now contain the room content
    disk = (repo / SPEC_FILENAME).read_text()
    assert disk == expected_md
    # The event SHA must equal the new HEAD
    new_head = _git_head(repo)
    assert evs[0].artifact_commit_sha == new_head


# ---------------------------------------------------------------------------
# Test 2a: collab empty-check reads the room, not stale disk — room has content → lock succeeds
# ---------------------------------------------------------------------------


def test_collab_lock_stale_empty_disk_but_room_nonempty(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Disk proposal-spec.md is empty but the room has content → checkpoint writes
    non-empty content to disk, so the lock SUCCEEDS (post-checkpoint empty-check passes)."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    # Commit an EMPTY spec file to disk
    _init_spec_repo(tmp_path, ws_id, spec_content="   \n")

    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    # Seed a non-empty room update
    _seed_spec_room_update(pg_session_factory, ws_id, owner_id, "## Aims\n\ncontent.\n")

    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    # Checkpoint wrote non-empty spec to disk → lock succeeds
    assert r.status_code == 200, r.text
    assert r.json()["locked"] is True


# ---------------------------------------------------------------------------
# Test 2b: collab empty-check — checkpoint writes whitespace to disk → 409
# ---------------------------------------------------------------------------


def test_collab_lock_post_checkpoint_empty_is_409(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Collab enabled; checkpoint writes whitespace-only content to disk (simulating
    a room whose serialized markdown is empty) → post-checkpoint empty-check fires → 409."""
    from call_manager.proposal_assist.collab.checkpoint import CheckpointResult
    from call_manager.proposal_assist.paths import workspace_repo_dir

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    repo = workspace_repo_dir(str(tmp_path), ws_id)
    # Write the spec file on disk (pre-mutex exists() check) and commit it
    env = _git_env()
    (repo / SPEC_FILENAME).write_text("## Aims\noriginal.\n")
    subprocess.run(["git", "add", SPEC_FILENAME], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed spec"], cwd=repo, check=True, env=env)
    head = _git_head(repo)

    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    # Monkeypatch checkpoint_artifact to simulate a checkpoint that writes empty
    # content to disk (the room's serialized markdown is whitespace-only).
    def _fake_checkpoint_writes_empty(session, *, workspace_id, artifact, workspaces_dir, **kw):
        # Write whitespace-only content to the spec file (simulating an empty room)
        repo_path = workspace_repo_dir(workspaces_dir, workspace_id)
        (repo_path / SPEC_FILENAME).write_text("   \n")
        # Return a valid (non-empty) SHA so the frozen_sha guard passes
        return CheckpointResult(committed=True, commit_sha=head, authors=[owner_id])

    monkeypatch.setattr(
        "call_manager.web.api.workspaces.checkpoint_artifact", _fake_checkpoint_writes_empty
    )

    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# Test 3: collab unlock stamps a fresh checkpoint SHA
# ---------------------------------------------------------------------------


def test_collab_unlock_stamps_fresh_checkpoint_sha(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Collab enabled; lock spec; seed an uncheckpointed room edit; unlock →
    the unlock SpecLockEvent stamps a NEW checkpoint SHA."""
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    repo = _init_spec_repo(tmp_path, ws_id, spec_content="## Aims\noriginal.\n")

    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    c = _client(app, owner_id)
    # Lock first (no room edits yet — checkpoint is a no-op, returns current HEAD)
    r_lock = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r_lock.status_code == 200, r_lock.text
    head_after_lock = _git_head(repo)

    # Seed a room edit AFTER the lock
    _seed_spec_room_update(pg_session_factory, ws_id, owner_id, "## Aims\n\nupdated post-lock.\n")

    r_unlock = c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    assert r_unlock.status_code == 200, r_unlock.text
    assert r_unlock.json()["locked"] is False

    evs = _spec_lock_events(pg_session_factory, ws_id)
    unlock_ev = next(e for e in evs if e.action == "unlock")
    # The unlock should have checkpointed the post-lock edit → new SHA
    assert unlock_ev.artifact_commit_sha != head_after_lock, (
        "Unlock should checkpoint the post-lock edit, producing a new SHA"
    )
    new_head = _git_head(repo)
    assert unlock_ev.artifact_commit_sha == new_head


# ---------------------------------------------------------------------------
# Test 4: collab empty-SHA → 404 (Codex Medium)
# ---------------------------------------------------------------------------


def test_collab_lock_no_committable_head_is_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Collab enabled; workspace has NO git repo (collab enabled pre-repo) →
    checkpoint_artifact returns commit_sha="" → POST spec/lock → 404 (not an
    empty-SHA lock).  Spec file must exist on disk for the pre-mutex check to pass."""
    from call_manager.proposal_assist.paths import workspace_repo_dir

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")

    # Write the spec FILE directly to disk (no git commit — the init_repo from
    # _seed only commits proposal.md, and we do NOT call _init_spec_repo so there
    # is a repo but no spec commit yet... but we need collab AND no committable HEAD.
    # Simplest: write the spec file on disk (so the pre-mutex exists() check passes)
    # then monkeypatch checkpoint_artifact to return commit_sha="" (mirrors the
    # no-repo case without destroying the repo _seed already created).
    repo = workspace_repo_dir(str(tmp_path), ws_id)
    (repo / SPEC_FILENAME).write_text("## Aims\ncontent.\n")
    # Do NOT git-add/commit it — spec file exists on disk but is untracked.

    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    # Seed an authored update so checkpoint has something to try to commit
    _seed_spec_room_update(pg_session_factory, ws_id, owner_id, "## Aims\n\ncontent.\n")

    # Monkeypatch checkpoint_artifact to simulate "no committable HEAD" (returns commit_sha="")
    from call_manager.proposal_assist.collab.checkpoint import CheckpointResult

    def _fake_checkpoint(*args, **kwargs):
        return CheckpointResult(committed=False, commit_sha="", authors=[])

    monkeypatch.setattr(
        "call_manager.web.api.workspaces.checkpoint_artifact", _fake_checkpoint
    )

    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "not_found"
    # No lock event should have been written
    assert _spec_lock_events(pg_session_factory, ws_id) == []


def test_collab_unlock_no_committable_head_is_404(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Symmetric to the lock case: collab enabled, checkpoint returns empty SHA →
    POST spec/unlock → 404."""
    from call_manager.proposal_assist.paths import workspace_repo_dir

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")

    # First lock the spec via the flag-OFF path (no collab yet) so there's something to unlock
    repo = workspace_repo_dir(str(tmp_path), ws_id)
    (repo / SPEC_FILENAME).write_text("## Aims\ncontent.\n")
    env = _git_env()
    subprocess.run(["git", "add", SPEC_FILENAME], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed spec"], cwd=repo, check=True, env=env)

    # Lock without collab (flag-off path — no checkpoint needed)
    c = _client(app, owner_id)
    r_lock = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r_lock.status_code == 200, r_lock.text

    # Now enable collab
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="spec")
        s.commit()

    # Monkeypatch checkpoint_artifact to return empty SHA
    from call_manager.proposal_assist.collab.checkpoint import CheckpointResult

    def _fake_checkpoint(*args, **kwargs):
        return CheckpointResult(committed=False, commit_sha="", authors=[])

    monkeypatch.setattr(
        "call_manager.web.api.workspaces.checkpoint_artifact", _fake_checkpoint
    )

    r_unlock = c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    assert r_unlock.status_code == 404, r_unlock.text
    assert r_unlock.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Test 5: flag-OFF parity guard (collab disabled → current behaviour unchanged)
# ---------------------------------------------------------------------------


def test_flag_off_lock_unchanged(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """When collab is NOT enabled, lock behaves exactly as before (no checkpoint call)."""
    from unittest.mock import patch

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    # Collab deliberately NOT enabled

    with patch("call_manager.web.api.workspaces.checkpoint_artifact") as mock_cp:
        c = _client(app, owner_id)
        r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
        assert r.status_code == 200, r.text
        assert r.json()["locked"] is True
        mock_cp.assert_not_called()
