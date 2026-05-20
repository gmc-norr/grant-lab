import asyncio
import uuid


def test_claude_adapter_constructor_accepts_tools_factory():
    """The adapter accepts a tools_factory; if None, it works as before."""
    from call_manager.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
    )

    adapter = ClaudeAgentSDKAdapter(model="x", workspaces_dir="/tmp")
    assert adapter is not None
    adapter2 = ClaudeAgentSDKAdapter(
        model="x",
        workspaces_dir="/tmp",
        tools_factory=lambda repo_dir, workspace_id: None,  # type: ignore[arg-type]
    )
    assert adapter2 is not None


def test_factory_wires_tools_into_adapter(monkeypatch, _settings_env):
    """build_workspace_controller passes a tools_factory closure to the
    claude adapter; the closure is what builds the per-workspace MCP
    server."""
    from call_manager.config import Settings
    from call_manager.proposal_assist.drafting.factory import (
        build_workspace_controller,
    )

    monkeypatch.delenv("DRAFTING_ADAPTER", raising=False)  # default = claude
    monkeypatch.setenv("WORKSPACES_DIR", "/tmp/test_workspaces")
    settings = Settings()

    controller = build_workspace_controller(
        settings=settings,
        session_factory=None,  # type: ignore[arg-type]
    )
    adapter = controller._registry._adapter
    assert getattr(adapter, "_tools_factory", None) is not None


def test_adapter_launch_passes_mcp_servers_and_allowed_tools(monkeypatch):
    """When tools_factory is set, adapter.launch passes
    ClaudeAgentOptions(mcp_servers=..., tools=[], strict_mcp_config=True,
    allowed_tools=<bare names>)."""
    from call_manager.proposal_assist.drafting import claude_adapter as ca
    from call_manager.proposal_assist.drafting.contract import LaunchContext

    captured: dict = {}
    real_options = ca.ClaudeAgentOptions

    def spy_options(**kw):
        captured.update(kw)
        return real_options(**kw)

    monkeypatch.setattr(ca, "ClaudeAgentOptions", spy_options)

    class _StubClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

    monkeypatch.setattr(ca, "ClaudeSDKClient", _StubClient)

    sentinel_server = object()

    def fake_factory(repo_dir, workspace_id):
        return sentinel_server

    adapter = ca.ClaudeAgentSDKAdapter(
        model="x",
        workspaces_dir="/tmp",
        tools_factory=fake_factory,  # type: ignore[arg-type]
    )
    ctx = LaunchContext(workspace_id=uuid.uuid4())
    asyncio.run(adapter.launch(ctx))

    mcp = captured.get("mcp_servers")
    assert mcp is not None
    assert "proposal_assist" in mcp
    assert mcp["proposal_assist"] is sentinel_server

    # Built-in toolbox disabled (Codex B2): tools=[] + strict_mcp_config=True.
    assert captured.get("tools") == []
    assert captured.get("strict_mcp_config") is True

    allowed = captured.get("allowed_tools")
    assert allowed is not None
    # Bare names per SDK docstring example (see Task 7 Step 0).
    assert "read_proposal" in allowed
    assert "edit_proposal" in allowed
    # NO mcp__ prefix — confirm we're using the bare form.
    assert not any(t.startswith("mcp__") for t in allowed)
