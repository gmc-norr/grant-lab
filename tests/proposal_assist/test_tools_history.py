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


@pytest.mark.asyncio
async def test_diff_artifact_rejects_option_injection_via_output_flag(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """Codex security regression: a value like `--output=proposal.md`
    must be rejected BEFORE reaching git. Without the sha-pattern guard
    git would interpret it as the --output option and write the diff to
    proposal.md, bypassing edit_proposal/replace_proposal entirely."""
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)

    original_proposal = (repo_dir / "proposal.md").read_text()

    result = await diff_artifact_impl(
        ctx,
        commit_a="--output=proposal.md",
        commit_b=shas[-1],
    )
    assert result.get("error")
    assert "hex commit sha" in result["error"]
    # proposal.md must be untouched.
    assert (repo_dir / "proposal.md").read_text() == original_proposal

    # Also test commit_b position.
    result = await diff_artifact_impl(
        ctx,
        commit_a=shas[0],
        commit_b="--output=proposal.md",
    )
    assert result.get("error")
    assert "hex commit sha" in result["error"]
    assert (repo_dir / "proposal.md").read_text() == original_proposal


@pytest.mark.asyncio
async def test_read_artifact_at_rejects_option_injection(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """Same class of attack via git show — reject before invoking git."""
    repo_dir = tmp_path / "repo"
    _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)

    result = await read_artifact_at_impl(
        ctx,
        commit_sha="--output=evil.txt",
    )
    assert result.get("error")
    assert "hex commit sha" in result["error"]
    # No evil.txt written.
    assert not (repo_dir / "evil.txt").exists()


@pytest.mark.asyncio
async def test_read_artifact_at_rejects_non_hex_input(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """Non-hex strings (random branch names, garbage) are rejected
    by the regex even though they're not malicious — the API contract
    is sha-only."""
    repo_dir = tmp_path / "repo"
    _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)

    for bad in ("HEAD", "main", "xyz123", "abc", "", "deadbeefXX"):
        result = await read_artifact_at_impl(ctx, commit_sha=bad)
        assert result.get("error"), f"{bad!r} should have been rejected"
        assert "hex commit sha" in result["error"]


@pytest.mark.asyncio
async def test_diff_artifact_accepts_valid_shas(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """The validator must not regress valid-input acceptance: shas
    from get_artifact_history are valid by construction."""
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_history(repo_dir)
    ctx = _build_ctx(repo_dir, pg_session_factory)

    # Full 40-char sha.
    result = await diff_artifact_impl(ctx, commit_a=shas[0], commit_b=shas[-1])
    assert "diff" in result
    assert "error" not in result

    # Abbreviated 7-char sha (git's default short form).
    result = await diff_artifact_impl(
        ctx,
        commit_a=shas[0][:7],
        commit_b=shas[-1][:7],
    )
    assert "diff" in result
    assert "error" not in result
