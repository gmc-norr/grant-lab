"""Shared fixtures for proposal_assist/review tests."""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from grant_lab.db.models import ReviewRun, ReviewRunItem
from tests.proposal_assist.conftest import make_workspace


@pytest.fixture
def review_workspace(tmp_path, pg_session_factory):
    """Yield (session, workspace_id, workspaces_dir, repo_path).

    Creates a Workspace row with current_phase="active", non-empty
    rubric_criteria, and rubric_funder_notes. Initialises a bare git
    repo at <workspaces_dir>/<ws_id>/repo so context tests can commit
    files without further setup.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, current_phase="active")
        ws.rubric_criteria = [
            {"name": "Scientific quality", "description": "Rigour and novelty", "weight": 0.6},
            {"name": "Impact", "description": "Clinical and societal reach", "weight": 0.4},
        ]
        ws.rubric_funder_notes = "Swedish university-hospital context."
        s.flush()
        ws_id = ws.id
        s.commit()

    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True
    )

    with pg_session_factory() as s:
        yield s, ws_id, tmp_path, repo


@pytest.fixture
def review_run_with_item(pg_session_factory):
    """Yield (session_factory, workspace_id, review_run_id, item_id).

    Creates a ReviewRun (status="running") and one ReviewRunItem
    (kind="fit_check", status="running"), committing so a separate
    session can see the rows.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, current_phase="active")
        ws.rubric_criteria = [{"name": "Quality", "weight": 1.0}]
        ws.rubric_funder_notes = "Test funder notes."
        s.flush()
        run = ReviewRun(workspace_id=ws.id, status="running", draft_sha="x" * 64)
        s.add(run)
        s.flush()
        item = ReviewRunItem(
            review_run_id=run.id, kind="fit_check", status="running"
        )
        s.add(item)
        s.commit()
        ws_id, run_id, item_id = ws.id, run.id, item.id

    yield pg_session_factory, ws_id, run_id, item_id


@pytest.fixture
def review_active_workspace(tmp_path, pg_session_factory):
    """Yield (session_factory, ws_id, workspaces_dir, repo_path, draft_sha).

    Creates a Workspace row with current_phase="active" and rubric_criteria,
    then initialises a git repo containing committed proposal.md and
    proposal-spec.md. draft_sha is the sha256 of the proposal.md bytes.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, current_phase="active")
        ws.rubric_criteria = [
            {"name": "Scientific quality", "description": "Rigour and novelty", "weight": 0.6},
            {"name": "Impact", "description": "Clinical and societal reach", "weight": 0.4},
        ]
        ws.rubric_funder_notes = "Swedish university-hospital context."
        s.flush()
        ws_id = ws.id
        s.commit()

    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    proposal_bytes = b"# Draft\n\nThis is the draft proposal text.\n"
    (repo / "proposal.md").write_bytes(proposal_bytes)
    (repo / "proposal-spec.md").write_text("## Aims\n\nSpec text.\n")

    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True)

    draft_sha = hashlib.sha256(proposal_bytes).hexdigest()

    yield pg_session_factory, ws_id, tmp_path, repo, draft_sha
