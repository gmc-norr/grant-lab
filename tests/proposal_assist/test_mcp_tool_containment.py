"""Tests that background sub-agent adapters hard-deny non-allowlisted MCP tools.

DEFECT 1 fix: disallowed_tools strips denied tools from model context, and
can_use_tool callback refuses calls not in the role's allowlist — defense-in-depth
behind the existing allowed_tools/strict_mcp_config boundary.
"""
import asyncio
import uuid
from pathlib import Path


def _build_options_for_role(allowed_tools: list[str]) -> dict:
    """Build ClaudeAgentOptions kwargs for a background role with a
    minimal tools_factory (no real DB needed — just building the options dict)."""
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.drafting.contract import LaunchContext
    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        build_proposal_assist_mcp_server,
    )

    ctx = ToolContext(
        workspace_id=uuid.UUID(int=0),
        repo_dir=Path("/tmp"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )

    def _factory(repo_dir, ws_id):
        return build_proposal_assist_mcp_server(ctx)

    adapter = ClaudeAgentSDKAdapter(
        model="test",
        workspaces_dir="/tmp",
        tools_factory=_factory,
        allowed_tools=allowed_tools,
    )
    return adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))


def test_evaluator_disallowed_tools_excludes_fetch_reference_and_propose():
    """Evaluator role (submit_scorecard only) must hard-deny fetch_reference,
    propose_edit, propose_spec_edit, and all other non-submit tools."""
    kwargs = _build_options_for_role(["submit_scorecard"])

    disallowed = set(kwargs.get("disallowed_tools", []))
    assert "fetch_reference" in disallowed, "fetch_reference must be hard-denied for evaluator"
    assert "propose_edit" in disallowed, "propose_edit must be hard-denied for evaluator"
    assert "propose_spec_edit" in disallowed, "propose_spec_edit must be hard-denied for evaluator"
    # The only allowed tool must NOT be in the disallowed set
    assert "submit_scorecard" not in disallowed


def test_review_disallowed_tools_excludes_dangerous_tools():
    """Review role (submit_review only) must hard-deny fetch_reference and propose_*."""
    kwargs = _build_options_for_role(["submit_review"])

    disallowed = set(kwargs.get("disallowed_tools", []))
    assert "fetch_reference" in disallowed
    assert "propose_edit" in disallowed
    assert "submit_review" not in disallowed


def test_uncited_claims_disallowed_tools_excludes_dangerous_tools():
    """Uncited-claims role (submit_uncited_claims only) must hard-deny fetch_reference."""
    kwargs = _build_options_for_role(["submit_uncited_claims"])

    disallowed = set(kwargs.get("disallowed_tools", []))
    assert "fetch_reference" in disallowed
    assert "propose_edit" in disallowed
    assert "submit_uncited_claims" not in disallowed


def test_disallowed_plus_allowed_covers_all_mcp_tools():
    """disallowed_tools ∪ allowed_tools == all MCP tools (no gaps, no overlaps)."""
    from call_manager.proposal_assist.tools.registry import collect_mcp_tool_names

    allowed_list = ["submit_scorecard"]
    kwargs = _build_options_for_role(allowed_list)

    allowed = set(kwargs["allowed_tools"])
    disallowed = set(kwargs.get("disallowed_tools", []))
    all_mcp = collect_mcp_tool_names()

    assert allowed | disallowed == all_mcp, (
        f"gap: {all_mcp - allowed - disallowed}  overlap: {allowed & disallowed}"
    )
    assert allowed & disallowed == set(), "allowed and disallowed must be disjoint"


def test_can_use_tool_denies_non_allowlisted():
    """The can_use_tool callback must deny tools not in the role's allowlist."""
    kwargs = _build_options_for_role(["submit_scorecard"])
    callback = kwargs.get("can_use_tool")
    assert callback is not None, "can_use_tool callback must be wired for background roles"

    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    # Allowed tool → Allow
    result_allow = asyncio.run(callback("submit_scorecard", {}, None))
    assert isinstance(result_allow, PermissionResultAllow)

    # Non-allowed tool → Deny
    result_deny = asyncio.run(callback("fetch_reference", {}, None))
    assert isinstance(result_deny, PermissionResultDeny)

    result_deny2 = asyncio.run(callback("propose_edit", {}, None))
    assert isinstance(result_deny2, PermissionResultDeny)


def test_interactive_adapter_has_no_disallowed_tools():
    """The interactive adapter (allowed_tools=None) must NOT set disallowed_tools
    — it keeps the full MCP surface available."""
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.drafting.contract import LaunchContext
    from call_manager.proposal_assist.tools.registry import (
        ToolContext,
        build_proposal_assist_mcp_server,
    )

    ctx = ToolContext(
        workspace_id=uuid.UUID(int=0),
        repo_dir=Path("/tmp"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )

    adapter = ClaudeAgentSDKAdapter(
        model="test",
        workspaces_dir="/tmp",
        tools_factory=lambda repo_dir, ws_id: build_proposal_assist_mcp_server(ctx),
        # No allowed_tools supplied → interactive default
    )
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    assert "disallowed_tools" not in kwargs
    assert "can_use_tool" not in kwargs


def test_background_agents_use_role_scoped_allowlist():
    """Verify evaluator/review/uncited-claims builders pass role-scoped allowed_tools
    that trigger the hard-deny path in _build_options_kwargs."""
    from call_manager.proposal_assist.evaluator.agent import evaluator_allowed_tools
    from call_manager.proposal_assist.review.agent import review_allowed_tools
    from call_manager.proposal_assist.uncited_claims.agent import uncited_claims_allowed_tools

    assert evaluator_allowed_tools() == ["submit_scorecard"]
    assert review_allowed_tools() == ["submit_review"]
    assert uncited_claims_allowed_tools() == ["submit_uncited_claims"]
