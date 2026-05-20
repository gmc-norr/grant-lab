import uuid
from pathlib import Path

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
