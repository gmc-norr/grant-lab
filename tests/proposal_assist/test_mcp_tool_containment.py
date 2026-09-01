"""Tests that background sub-agent adapters hard-deny non-allowlisted MCP tools.

DEFECT 1 fix: disallowed_tools strips denied tools from model context, and
can_use_tool callback refuses calls not in the role's allowlist — defense-in-depth
behind the existing allowed_tools/strict_mcp_config boundary.
"""
import asyncio
import uuid
from pathlib import Path

import pytest

from call_manager.proposal_assist.tools.registry import (
    ToolContext,
    _deny_if_out_of_turn,
    _safe_tool,
)

_MCP_PREFIX = "mcp__proposal_assist__"


def _qualified(tool_name: str) -> str:
    return f"{_MCP_PREFIX}{tool_name}"


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
    assert _qualified("fetch_reference") in disallowed, (
        "fetch_reference must be hard-denied for evaluator"
    )
    assert _qualified("propose_edit") in disallowed, (
        "propose_edit must be hard-denied for evaluator"
    )
    assert _qualified("propose_spec_edit") in disallowed, (
        "propose_spec_edit must be hard-denied for evaluator"
    )
    # The only allowed tool must NOT be in the disallowed set
    assert _qualified("submit_scorecard") not in disallowed


def test_review_disallowed_tools_excludes_dangerous_tools():
    """Review role (submit_review only) must hard-deny fetch_reference and propose_*."""
    kwargs = _build_options_for_role(["submit_review"])

    disallowed = set(kwargs.get("disallowed_tools", []))
    assert _qualified("fetch_reference") in disallowed
    assert _qualified("propose_edit") in disallowed
    assert _qualified("submit_review") not in disallowed


def test_uncited_claims_disallowed_tools_excludes_dangerous_tools():
    """Uncited-claims role (submit_uncited_claims only) must hard-deny fetch_reference."""
    kwargs = _build_options_for_role(["submit_uncited_claims"])

    disallowed = set(kwargs.get("disallowed_tools", []))
    assert _qualified("fetch_reference") in disallowed
    assert _qualified("propose_edit") in disallowed
    assert _qualified("submit_uncited_claims") not in disallowed


def test_disallowed_plus_allowed_covers_all_mcp_tools():
    """disallowed_tools ∪ allowed_tools == all MCP tools (no gaps, no overlaps)."""
    from call_manager.proposal_assist.tools.registry import collect_mcp_tool_names

    allowed_list = ["submit_scorecard"]
    kwargs = _build_options_for_role(allowed_list)

    allowed = set(kwargs["allowed_tools"])
    disallowed = set(kwargs.get("disallowed_tools", []))
    all_mcp = {_qualified(name) for name in collect_mcp_tool_names()}

    assert allowed | disallowed == all_mcp, (
        f"gap: {all_mcp - allowed - disallowed}  overlap: {allowed & disallowed}"
    )
    assert allowed & disallowed == set(), "allowed and disallowed must be disjoint"


def test_can_use_tool_denies_non_allowlisted():
    """The can_use_tool callback must deny tools not in the role's allowlist.

    The Claude Agent SDK invokes can_use_tool with the FULLY-QUALIFIED MCP name
    (``mcp__<server>__<tool>``), not the bare name stored in allowed_tools. The
    callback must normalize both sides to the same shape so the allow-branch
    genuinely fires for the role's own tool (a real second defense layer), not
    just the deny-branch."""
    kwargs = _build_options_for_role(["submit_scorecard"])
    callback = kwargs.get("can_use_tool")
    assert callback is not None, "can_use_tool callback must be wired for background roles"

    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    _Q = "mcp__proposal_assist__"  # the prefix the SDK applies to MCP tools

    # Allowlisted tool, QUALIFIED name (the real SDK call shape) → Allow.
    result_allow = asyncio.run(callback(f"{_Q}submit_scorecard", {}, None))
    assert isinstance(result_allow, PermissionResultAllow), (
        "allowlisted tool (qualified name) must be allowed — the allow-branch "
        "must work against the SDK's real qualified call shape"
    )

    # The bare-name shape must also resolve (robust to either form).
    result_allow_bare = asyncio.run(callback("submit_scorecard", {}, None))
    assert isinstance(result_allow_bare, PermissionResultAllow)

    # Non-allowlisted tools, QUALIFIED names → Deny.
    result_deny = asyncio.run(callback(f"{_Q}fetch_reference", {}, None))
    assert isinstance(result_deny, PermissionResultDeny)

    result_deny2 = asyncio.run(callback(f"{_Q}propose_edit", {}, None))
    assert isinstance(result_deny2, PermissionResultDeny)

    result_deny3 = asyncio.run(callback(f"{_Q}propose_citation", {}, None))
    assert isinstance(result_deny3, PermissionResultDeny)

    result_deny4 = asyncio.run(callback(f"{_Q}propose_spec_edit", {}, None))
    assert isinstance(result_deny4, PermissionResultDeny)


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


# ---------------------------------------------------------------------------
# Per-turn runtime gate tests (Task 4)
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_ctx() -> ToolContext:
    # Built inline exactly like the existing containment tests (_build_options_for_role):
    # no DB needed — these tests only exercise the runtime gate.
    return ToolContext(
        workspace_id=uuid.UUID(int=0),
        repo_dir=Path("/tmp"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )


def test_deny_helper_gates_only_when_list_set(tool_ctx):
    tool_ctx.allowed_tool_names = None
    assert _deny_if_out_of_turn("propose_edit", tool_ctx) is None  # no per-turn list
    tool_ctx.allowed_tool_names = {"read_proposal"}
    assert _deny_if_out_of_turn("read_proposal", tool_ctx) is None
    assert _deny_if_out_of_turn("propose_edit", tool_ctx) is not None  # denied


@pytest.mark.asyncio
async def test_safe_tool_denies_out_of_turn_tool(tool_ctx):
    tool_ctx.allowed_tool_names = {"read_proposal"}

    async def _impl():
        return {"ok": True}

    denied = await _safe_tool("propose_edit", tool_ctx, _impl)
    assert "not allowed" in str(denied).lower()
    allowed = await _safe_tool("read_proposal", tool_ctx, _impl)
    assert "not allowed" not in str(allowed).lower()


@pytest.mark.asyncio
async def test_safe_tool_no_gate_when_none(tool_ctx):
    tool_ctx.allowed_tool_names = None

    async def _impl():
        return {"ok": True}

    out = await _safe_tool("propose_edit", tool_ctx, _impl)
    assert "not allowed" not in str(out).lower()
