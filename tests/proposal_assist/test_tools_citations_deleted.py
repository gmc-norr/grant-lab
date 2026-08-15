"""Agent citation tools refuse a deleted workspace (Slice B-mutex).

These run as MCP tools inside the SDK subprocess, so the refusal follows the
module's established {"error": ...} contract rather than raising an opaque tool
crash at the agent — mirroring the CommitMutexTimeout branch two lines away.
"""

import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import DELETED_ERROR_MESSAGE
from call_manager.proposal_assist.tools.citations import insert_citation_impl
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace

PROPOSAL = "# Proposal\n\nWe argue that something is true in our population.\n"


def _init_repo(repo_dir: Path, initial_content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(initial_content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _head(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def deleted_ctx(pg_session_factory, _settings_env, tmp_path):
    """A ToolContext for a workspace that has been soft-deleted."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, PROPOSAL)
    with pg_session_factory() as s:
        s.get(Workspace, wsid).soft_deleted_at = datetime.now(UTC)
        s.commit()
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=Settings(),
    )
    return ctx, repo_dir


@pytest.mark.asyncio
async def test_insert_citation_refuses(deleted_ctx):
    ctx, repo_dir = deleted_ctx
    head = _head(repo_dir)
    result = await insert_citation_impl(
        ctx,
        anchor="something is true",
        reference_id=str(uuid.uuid4()),
        chunk_index=0,
        evidence_quote="q",
    )
    assert result == {"error": DELETED_ERROR_MESSAGE}
    assert _head(repo_dir) == head  # no commit
