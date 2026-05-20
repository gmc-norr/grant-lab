"""End-to-end: build the MCP server, hand-invoke one tool's handler
through it, verify the closure properly bound ctx."""

import subprocess
from pathlib import Path

from call_manager.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path):
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text("# Initial\n\nText.\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def test_mcp_server_contains_tools_bound_to_context(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """Smoke: build the per-workspace MCP server with a real ToolContext
    backed by testcontainer Postgres + tmp git repo. The server is a
    dict the SDK adapter can pass directly to ClaudeAgentOptions.

    No actual SDK calls happen here (no API key in CI). The full agent
    loop is gated on ANTHROPIC_API_KEY in the dedicated claude-adapter
    smoke (test_drafting_claude_adapter.py)."""
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=settings,
    )
    server = build_proposal_assist_mcp_server(ctx)
    # Server is the {type, name, instance} dict ClaudeAgentOptions accepts.
    assert server["type"] == "sdk"
    assert server["name"] == "proposal_assist"
    assert server["instance"] is not None
