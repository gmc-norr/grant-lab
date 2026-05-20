import subprocess
from pathlib import Path

import pytest

from call_monitor.proposal_assist.tools.history import (
    diff_artifact_impl,
    get_artifact_history_impl,
    read_artifact_at_impl,
)
from call_monitor.proposal_assist.tools.registry import ToolContext


def _init_repo_with_history(repo_dir: Path) -> list[str]:
    """Create a repo with three commits; return shas in chronological order."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)

    shas: list[str] = []
    for i, line in enumerate(["initial", "second", "third"]):
        (repo_dir / "proposal.md").write_text(f"# Proposal v{i}\n{line}\n")
        subprocess.run(["git", "add", "proposal.md"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"commit {i}"], cwd=repo_dir, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        shas.append(sha)
    return shas


def _build_ctx(repo_dir, pg_session_factory):
    import uuid as _uuid

    from call_monitor.config import Settings

    return ToolContext(
        workspace_id=_uuid.uuid4(),
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=Settings.model_construct(),
    )


@pytest.mark.asyncio
async def test_get_artifact_history_returns_recent_commits(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await get_artifact_history_impl(ctx, limit=10)
    commits = result["commits"]
    assert len(commits) == 3
    # Returned newest-first.
    assert commits[0]["sha"] == shas[-1]
    assert commits[-1]["sha"] == shas[0]
    assert commits[0]["message"] == "commit 2"


@pytest.mark.asyncio
async def test_get_artifact_history_respects_limit(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    repo_dir = tmp_path / "repo"
    _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await get_artifact_history_impl(ctx, limit=2)
    assert len(result["commits"]) == 2


@pytest.mark.asyncio
async def test_read_artifact_at_returns_old_content(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await read_artifact_at_impl(ctx, commit_sha=shas[0])
    assert "v0" in result["content"]


@pytest.mark.asyncio
async def test_read_artifact_at_rejects_invalid_sha(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    repo_dir = tmp_path / "repo"
    _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await read_artifact_at_impl(ctx, commit_sha="deadbeef")
    assert result.get("error")


@pytest.mark.asyncio
async def test_diff_artifact_returns_unified_diff(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await diff_artifact_impl(ctx, commit_a=shas[0], commit_b=shas[-1])
    diff = result["diff"]
    assert "v0" in diff and "v2" in diff
    assert "+" in diff and "-" in diff


@pytest.mark.asyncio
async def test_diff_artifact_rejects_invalid_sha(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await diff_artifact_impl(ctx, commit_a="bad", commit_b=shas[-1])
    assert result.get("error")


@pytest.mark.asyncio
async def test_get_artifact_history_skips_commits_to_other_files(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """Rev3 / Codex M5 regression: commits to phased artifacts other
    than proposal.md (e.g., proposal-spec.md) MUST NOT appear in
    proposal.md's history."""
    repo_dir = tmp_path / "repo"
    _init_repo_with_history(repo_dir)
    # Make a commit that touches ONLY a sibling phased artifact.
    (repo_dir / "proposal-spec.md").write_text("# Spec\n")
    subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "agent: edit spec"],
        cwd=repo_dir,
        check=True,
    )

    ctx = _build_ctx(repo_dir, pg_session_factory)
    result = await get_artifact_history_impl(ctx, limit=10)
    returned_messages = [c["message"] for c in result["commits"]]
    assert "agent: edit spec" not in returned_messages
    assert "commit 2" in returned_messages  # the proposal.md commits remain
