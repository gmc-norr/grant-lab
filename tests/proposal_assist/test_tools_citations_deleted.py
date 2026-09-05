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

from grant_lab.config import Settings
from grant_lab.db.models import Workspace
from grant_lab.proposal_assist.citations.core import insert_citation_impl
from grant_lab.proposal_assist.drafting.locking import DELETED_ERROR_MESSAGE
from grant_lab.proposal_assist.ports.stamp import FenceStamp
from grant_lab.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from grant_lab.proposal_assist.tools.registry import ToolContext
from grant_lab.workspaces import restore_workspace_db, soft_delete_workspace_db
from tests.proposal_assist.conftest import make_user, make_workspace


def _stamp(ctx: ToolContext) -> FenceStamp:
    """The fencing identity insert_citation_impl now requires. These test
    workspaces are all at lifecycle_generation 0, so the stamp the caller would
    thread carries generation 0."""
    return FenceStamp(
        workspace_id=ctx.workspace_id,
        generation=0,
        active_turn_job_id=None,
        origin_job_id=None,
    )


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
        stamp=_stamp(ctx),
        anchor="something is true",
        reference_id=str(uuid.uuid4()),
        chunk_index=0,
        evidence_quote="q",
    )
    assert result == {"error": DELETED_ERROR_MESSAGE}
    assert _head(repo_dir) == head  # no commit


def _porcelain(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True
    ).stdout


@pytest.fixture
def restored_ctx(pg_session_factory, _settings_env, tmp_path):
    """A ToolContext whose workspace was deleted and then restored (generation
    0 → 2) AFTER the agent's turn formed its belief at generation 0."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        admin = make_user(s, email="restore-admin@umu.se")
        s.commit()
        wsid, admin_id = ws.id, admin.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, PROPOSAL)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=Settings(),
    )
    stale_stamp = _stamp(ctx)  # generation 0, minted before the delete
    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=wsid)
        s.commit()
    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=wsid, admin_id=admin_id)
    assert outcome.generation == 2
    return ctx, repo_dir, stale_stamp


@pytest.mark.asyncio
async def test_insert_citation_refuses_a_restored_workspace_with_a_stale_stamp(restored_ctx):
    """Review 2026-09-02 A10 F3: `_insert_citation_locked` takes Primitive B
    inside a try, which the static gate cannot see, and its only behavioural pin
    drove a DELETED workspace — the weaker property. A liveness-only regression
    (plain mutex + a fresh soft_deleted_at re-read) passed 85/85 and would commit
    proposal.md + citations.json into the RESTORED repo. This is the delete →
    restore → resume case: the workspace is live again, at generation 2, and the
    agent's stamp still says 0."""
    ctx, repo_dir, stale_stamp = restored_ctx
    head = _head(repo_dir)
    with ctx.session_factory() as s:
        assert s.get(Workspace, ctx.workspace_id).soft_deleted_at is None  # live again
    result = await insert_citation_impl(
        ctx,
        stamp=stale_stamp,
        anchor="something is true",
        reference_id=str(uuid.uuid4()),
        chunk_index=0,
        evidence_quote="q",
    )
    assert result == {"error": DELETED_ERROR_MESSAGE}
    assert _head(repo_dir) == head, "a stale actor committed into the restored repo"
    assert _porcelain(repo_dir) == "", "a stale actor left the worktree dirty"
