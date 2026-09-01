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
    allowed_tools=<fully-qualified MCP names>)."""
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
    prefix = "mcp__proposal_assist__"
    assert all(t.startswith(prefix) for t in allowed)
    # Full allowlist coverage: 21 tools — 19 post-SP2a-2 (6 mutating tools removed,
    # propose_edit + propose_citation added) plus SP2b-2b's read_spec + propose_spec_edit.
    expected_tools = {
        "read_proposal",
        # SP2a-2 — propose-only mutation
        "propose_edit",
        # SP2b-2b — spec artifact (read + propose-only)
        "read_spec",
        "propose_spec_edit",
        "propose_citation",
        "get_artifact_history",
        "read_artifact_at",
        "diff_artifact",
        "get_call_details",
        "get_profile",
        # Slice 2 — references
        "list_references",
        "search_references",
        "navigate_reference",
        "read_reference",
        "read_reference_section",
        "summarize_data",
        # Slice 3 — agent-side fetch
        "fetch_reference",
        # read-only citation discovery + audit
        "list_citations",
        "validate_citations",
        "list_unsupported_claims",
        # Slice 4 — bibliography rendering
        "build_bibliography",
    }
    assert set(allowed) == {f"{prefix}{name}" for name in expected_tools}


def test_options_strip_builtin_tools_even_without_tools_factory():
    """SECURITY: tools=[] + strict_mcp_config=True must be set UNCONDITIONALLY.

    An adapter built with tools_factory=None would otherwise expose Claude
    Code's full built-in bash/file toolbox in the workspace repo — a latent
    RCE-class footgun. We assert the built-ins are stripped even when no MCP
    factory is present, and that no MCP wiring leaks through. The default
    permission_mode is now "default" (the bypassPermissions footgun is gone).
    """
    from call_manager.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
    )
    from call_manager.proposal_assist.drafting.contract import LaunchContext

    adapter = ClaudeAgentSDKAdapter(
        model="x",
        workspaces_dir="/tmp",
        tools_factory=None,
    )
    ctx = LaunchContext(workspace_id=uuid.uuid4())
    kwargs = adapter._build_options_kwargs(ctx)

    assert kwargs["tools"] == []
    assert kwargs["strict_mcp_config"] is True
    # No factory ⇒ no MCP server and no allowlist wiring.
    assert "mcp_servers" not in kwargs
    assert "allowed_tools" not in kwargs
    # Default permission mode is now "default" (the bypassPermissions footgun is
    # gone). tools=[]/strict_mcp_config still strip the built-ins unconditionally.
    assert kwargs["permission_mode"] == "default"


def test_every_collected_tool_is_allowlisted(_settings_env):
    """permission_mode='default' blocks any non-allowlisted tool, so every tool
    the registry collects must be reachable by the adapter that is supposed to
    call it — or a turn would hang on a blocked call with no UI to approve.

    The interactive adapter's default allowlist covers every collected tool
    EXCEPT the background-only submit tools (submit_scorecard, submit_review),
    which share the MCP server but are allowed ONLY by their own background
    adapter (evaluator / review). The invariant is therefore:
    interactive allowlist == collected − background-only submit tools, and the
    background-only set is exactly the union of the background adapters'
    allowlists.
    """
    import uuid as _uuid

    from call_manager.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
    )
    from call_manager.proposal_assist.drafting.contract import LaunchContext
    from call_manager.proposal_assist.evaluator.agent import evaluator_allowed_tools
    from call_manager.proposal_assist.review.agent import review_allowed_tools
    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        _collect_tools,
        build_proposal_assist_mcp_server,
    )
    from call_manager.proposal_assist.uncited_claims.agent import uncited_claims_allowed_tools

    # Background-only submit tools, sourced from the background adapters that own
    # them (not a hand-maintained literal — so a new background tool that forgets
    # to update this set trips the assertions below).
    background_only = (
        set(evaluator_allowed_tools())
        | set(review_allowed_tools())
        | set(uncited_claims_allowed_tools())
    )

    wsid = _uuid.uuid4()
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=__import__("pathlib").Path("/tmp/x"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    collected = {t.name for t in _collect_tools(ctx)}

    adapter = ClaudeAgentSDKAdapter(
        model="x",
        workspaces_dir="/tmp",
        tools_factory=lambda repo_dir, workspace_id: build_proposal_assist_mcp_server(ctx),
    )
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=wsid))
    allowed = set(kwargs["allowed_tools"])
    qualified_interactive = {
        f"mcp__proposal_assist__{name}" for name in collected - background_only
    }
    allowed_bare = {name.removeprefix("mcp__proposal_assist__") for name in allowed}

    # The background-only tools really are in the shared MCP server (so the
    # background adapters can call them) ...
    assert background_only <= collected, (
        f"background submit tools not collected: {background_only - collected}"
    )
    # ... and the interactive default allowlist is exactly the rest. No collected
    # tool is silently unreachable.
    assert allowed == qualified_interactive
    missing = collected - allowed_bare - background_only
    assert missing == set(), f"collected tools reachable by no adapter: {missing}"
    assert len(allowed) == 21
