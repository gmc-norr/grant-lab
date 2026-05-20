import uuid
from pathlib import Path

import pytest

from call_monitor.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)


def test_tool_context_is_a_dataclass():
    """Smoke: ToolContext has the four expected fields."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    assert isinstance(ctx.workspace_id, uuid.UUID)
    assert isinstance(ctx.repo_dir, Path)


def test_build_mcp_server_returns_dict_with_expected_keys():
    """create_sdk_mcp_server returns a dict with {type, name, instance};
    McpSdkServerConfig is a TypedDict so isinstance() does not work."""
    ctx = ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    server = build_proposal_assist_mcp_server(ctx)
    assert isinstance(server, dict)
    assert server["type"] == "sdk"
    assert server["name"] == "proposal_assist"
    assert "instance" in server


def _make_ctx():
    import uuid
    from pathlib import Path

    from call_monitor.proposal_assist.tools.registry import ToolContext

    return ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )


def test_collect_tools_returns_eight_named_tools():
    """All eight tool names are present. Probes _collect_tools directly
    instead of fighting the MCP Server's decorator-based API."""
    from call_monitor.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    names = {t.name for t in tools}
    assert names == {
        "read_proposal",
        "edit_proposal",
        "replace_proposal",
        "get_artifact_history",
        "read_artifact_at",
        "diff_artifact",
        "get_call_details",
        "get_profile",
    }


@pytest.mark.asyncio
async def test_tool_handler_returns_mcp_envelope_shape():
    """Smoke: a tool handler returns the MCP envelope shape, not the
    plain dict the *_impl function produces. Use read_proposal against
    a missing repo so the impl returns an error — we only care about
    envelope shape, not content."""
    from call_monitor.proposal_assist.tools.registry import _collect_tools

    tools = _collect_tools(_make_ctx())
    read = next(t for t in tools if t.name == "read_proposal")
    result = await read.handler({})
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert "is_error" in result
    # The "missing file" path is an error.
    assert result["is_error"] is True


@pytest.mark.asyncio
async def test_tool_handler_envelope_success_path():
    """When the impl returns a plain success dict (no 'error' key), the
    wrapper produces is_error=False and JSON-encodes the payload as
    the text content."""
    import json
    import subprocess
    import tempfile
    import uuid
    from pathlib import Path

    from call_monitor.proposal_assist.tools.registry import (
        ToolContext,
        _collect_tools,
    )

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "proposal.md").write_text("hello world")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        ctx = ToolContext(
            workspace_id=uuid.uuid4(),
            repo_dir=repo,
            session_factory=None,
            settings=None,  # type: ignore[arg-type]
        )
        tools = _collect_tools(ctx)
        read = next(t for t in tools if t.name == "read_proposal")
        result = await read.handler({})

    assert result["is_error"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["content"] == "hello world"
