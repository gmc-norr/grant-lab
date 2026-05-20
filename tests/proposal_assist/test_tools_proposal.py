import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from call_monitor.db.models import ArtifactVersion
from call_monitor.proposal_assist.tools.proposal import (
    edit_proposal_impl,
    read_proposal_impl,
    replace_proposal_impl,
)
from call_monitor.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, initial_content: str = "# Proposal\n\nInitial.\n"):
    """Helper: create a git repo with an initial proposal.md commit."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text(initial_content)
    subprocess.run(["git", "add", "proposal.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _build_ctx(pg_session_factory, tmp_path) -> tuple[ToolContext, uuid.UUID]:
    """Build a ToolContext backed by a real testcontainer DB + tmp repo."""
    from call_monitor.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    return ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=settings,
    ), wsid


@pytest.mark.asyncio
async def test_read_proposal_returns_current_content(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    ctx, _ = _build_ctx(pg_session_factory, tmp_path)
    result = await read_proposal_impl(ctx)
    assert result["content"] == "# Proposal\n\nInitial.\n"


@pytest.mark.asyncio
async def test_edit_proposal_applies_string_replace_and_commits(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Initial.", "new_string": "Revised content."}],
        triggering_turn_id=None,
    )
    assert (ctx.repo_dir / "proposal.md").read_text() == "# Proposal\n\nRevised content.\n"
    sha = result["commit_sha"]
    assert sha and len(sha) >= 7
    with pg_session_factory() as s:
        rows = list(s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)))
    assert len(rows) == 1
    assert rows[0].commit_sha == sha
    assert rows[0].created_by == "agent"


@pytest.mark.asyncio
async def test_edit_proposal_rejects_when_old_string_not_found(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "NOPE", "new_string": "X"}],
        triggering_turn_id=None,
    )
    assert result.get("error")
    assert (ctx.repo_dir / "proposal.md").read_text() == "# Proposal\n\nInitial.\n"
    with pg_session_factory() as s:
        rows = list(s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)))
    assert rows == []


@pytest.mark.asyncio
async def test_edit_proposal_rejects_when_old_string_appears_multiple_times(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    (ctx.repo_dir / "proposal.md").write_text("foo bar foo")
    subprocess.run(["git", "add", "proposal.md"], cwd=ctx.repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "setup"], cwd=ctx.repo_dir, check=True)

    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "foo", "new_string": "baz"}],
        triggering_turn_id=None,
    )
    assert "ambiguous" in (result.get("error") or "").lower()


@pytest.mark.asyncio
async def test_replace_proposal_overwrites_and_commits(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    new_content = "# Brand new\n\nFresh start.\n"
    result = await replace_proposal_impl(
        ctx,
        content=new_content,
        triggering_turn_id=None,
    )
    assert (ctx.repo_dir / "proposal.md").read_text() == new_content
    sha = result["commit_sha"]
    assert sha
    with pg_session_factory() as s:
        rows = list(s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)))
    assert len(rows) == 1
    assert rows[0].commit_sha == sha


@pytest.mark.asyncio
async def test_edit_proposal_no_op_when_edits_empty(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    result = await edit_proposal_impl(
        ctx,
        edits=[],
        triggering_turn_id=None,
    )
    assert result.get("error")
    with pg_session_factory() as s:
        rows = list(s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)))
    assert rows == []


@pytest.mark.asyncio
async def test_write_and_commit_rolls_back_working_tree_on_commit_failure(
    pg_session_factory,
    _settings_env,
    tmp_path,
    monkeypatch,
):
    """Codex C2 regression: working tree restored on commit failure."""
    import subprocess as _sp

    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    original = (ctx.repo_dir / "proposal.md").read_text()

    from call_monitor.proposal_assist.tools import proposal as _proposal_mod

    real_run = _proposal_mod.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "commit":
            raise _sp.CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr=b"simulated commit failure",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_proposal_mod.subprocess, "run", fake_run)

    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Initial.", "new_string": "Should not land."}],
        triggering_turn_id=None,
    )
    assert result.get("error")
    assert "commit failed" in result["error"]
    assert (ctx.repo_dir / "proposal.md").read_text() == original
    with pg_session_factory() as s:
        rows = list(s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid)))
    assert rows == []


@pytest.mark.asyncio
async def test_write_and_commit_rollback_leaves_unrelated_dirty_files_alone(
    pg_session_factory,
    _settings_env,
    tmp_path,
    monkeypatch,
):
    """Codex M4 regression: rollback MUST NOT touch unrelated tracked
    files that are dirty when our tool runs. Without scoped reset,
    `git reset --hard HEAD` would discard them."""
    import subprocess as _sp

    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)

    # Add an unrelated tracked file, commit it, then dirty it.
    sidecar = ctx.repo_dir / "sidecar.txt"
    sidecar.write_text("clean version")
    _sp.run(["git", "add", "sidecar.txt"], cwd=ctx.repo_dir, check=True)
    _sp.run(["git", "commit", "-q", "-m", "add sidecar"], cwd=ctx.repo_dir, check=True)
    sidecar.write_text("DIRTY uncommitted edits an admin left")

    from call_monitor.proposal_assist.tools import proposal as _proposal_mod

    real_run = _proposal_mod.subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "commit":
            raise _sp.CalledProcessError(
                returncode=1,
                cmd=cmd,
                stderr=b"simulated commit failure",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_proposal_mod.subprocess, "run", fake_run)

    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Initial.", "new_string": "Should not land."}],
        triggering_turn_id=None,
    )
    assert result.get("error")
    # Unrelated dirty file untouched.
    assert sidecar.read_text() == "DIRTY uncommitted edits an admin left"


@pytest.mark.asyncio
async def test_edit_proposal_links_triggering_turn(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    from call_monitor.db.models import ChatTurn

    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        turn = ChatTurn(
            workspace_id=wsid,
            role="user",
            content={"type": "text", "text": "make edits"},
        )
        s.add(turn)
        s.commit()
        turn_id = turn.id
    await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Initial.", "new_string": "Revised."}],
        triggering_turn_id=turn_id,
    )
    with pg_session_factory() as s:
        row = s.scalar(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid))
    assert row.triggering_turn_id == turn_id
