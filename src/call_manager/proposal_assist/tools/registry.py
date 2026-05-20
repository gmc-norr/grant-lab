"""ToolContext + MCP server builder for the per-workspace tool surface.

Tools are async functions decorated with claude_agent_sdk.tool(...) and
registered into an SDK MCP server via create_sdk_mcp_server. The server
runs in-process; the SDK calls handler functions over the MCP protocol
during a turn.

Each WorkspaceController.launch builds a fresh ToolContext and a fresh
MCP server bound to that workspace's repo_dir + session_factory. The
handlers are closures over ctx — they don't read from globals.

MCP return contract: tool handlers must return
    {"content": [{"type": "text", "text": "..."}], "is_error": False}
The SDK rejects plain dicts. _mcp_ok / _mcp_error wrap our plain-dict
*_impl returns into this envelope so the impl functions stay
unit-test-friendly.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpSdkServerConfig
    from sqlalchemy.orm import sessionmaker

    from call_manager.config import Settings


@dataclass
class ToolContext:
    """Per-workspace binding passed to every tool handler."""

    workspace_id: uuid.UUID
    repo_dir: Path
    session_factory: sessionmaker
    settings: Settings


def _mcp_ok(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a plain-dict tool result in the MCP content-block envelope.

    The SDK requires {"content": [{"type": "text", "text": "..."}],
    "is_error": False}. We JSON-encode the impl's dict so the model
    sees structured data; if the impl returned a single 'content' or
    'diff' string we still serialize the whole envelope as JSON for
    consistency — the model handles JSON fine.
    """
    return {
        "content": [{"type": "text", "text": json.dumps(data)}],
        "is_error": False,
    }


def _mcp_error(message: str) -> dict[str, Any]:
    """Wrap an error message in the MCP envelope with is_error=True."""
    return {
        "content": [{"type": "text", "text": message}],
        "is_error": True,
    }


def build_proposal_assist_mcp_server(ctx: ToolContext) -> McpSdkServerConfig:
    """Build the per-workspace MCP server. Each tool handler is a closure
    over ctx. The list grows as subsequent tasks (T2, T3, T4) add tools."""
    tools = _collect_tools(ctx)
    return create_sdk_mcp_server(name="proposal_assist", tools=tools)


def _wrap(impl_result: dict[str, Any]) -> dict[str, Any]:
    """Translate an *_impl plain-dict return into the MCP envelope."""
    if "error" in impl_result:
        return _mcp_error(impl_result["error"])
    return _mcp_ok(impl_result)


def _collect_tools(ctx: ToolContext) -> list:
    """Gather every tool's @tool-decorated handler.

    Rev3 NOTE: SDK converts dict-of-types into JSON Schema with ALL
    keys in `required`. To make args truly optional, every @tool below
    uses a full JSON Schema dict with explicit `required` lists.
    """
    from call_manager.proposal_assist.tools.context import (
        get_call_details_impl,
        get_profile_impl,
    )
    from call_manager.proposal_assist.tools.history import (
        diff_artifact_impl,
        get_artifact_history_impl,
        read_artifact_at_impl,
    )
    from call_manager.proposal_assist.tools.proposal import (
        edit_proposal_impl,
        read_proposal_impl,
        replace_proposal_impl,
    )

    @tool(
        "read_proposal",
        "Return the current proposal.md content.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def read_proposal(args):
        return _wrap(await read_proposal_impl(ctx))

    @tool(
        "edit_proposal",
        "Apply a list of {old_string, new_string} surgical edits to "
        "proposal.md. All-or-nothing: if any anchor is missing or "
        "ambiguous, no edits are applied. Auto-commits on success.",
        {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
                "triggering_turn_id": {"type": "string"},
            },
            "required": ["edits"],
        },
    )
    async def edit_proposal(args):
        from uuid import UUID

        ttid = args.get("triggering_turn_id") or None
        return _wrap(
            await edit_proposal_impl(
                ctx,
                edits=args["edits"],
                triggering_turn_id=UUID(ttid) if ttid else None,
            )
        )

    @tool(
        "replace_proposal",
        "Overwrite proposal.md with new content. For large structural rewrites. Auto-commits.",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "triggering_turn_id": {"type": "string"},
            },
            "required": ["content"],
        },
    )
    async def replace_proposal(args):
        from uuid import UUID

        ttid = args.get("triggering_turn_id") or None
        return _wrap(
            await replace_proposal_impl(
                ctx,
                content=args["content"],
                triggering_turn_id=UUID(ttid) if ttid else None,
            )
        )

    @tool(
        "get_artifact_history",
        "Return up to `limit` recent commits on proposal.md, newest first.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
            },
            "required": [],
        },
    )
    async def get_artifact_history(args):
        return _wrap(await get_artifact_history_impl(ctx, limit=int(args.get("limit", 20))))

    @tool(
        "read_artifact_at",
        "Return proposal.md content at a specific commit sha.",
        {
            "type": "object",
            "properties": {"commit_sha": {"type": "string"}},
            "required": ["commit_sha"],
        },
    )
    async def read_artifact_at(args):
        return _wrap(await read_artifact_at_impl(ctx, commit_sha=args["commit_sha"]))

    @tool(
        "diff_artifact",
        "Unified diff of proposal.md between two commit shas.",
        {
            "type": "object",
            "properties": {
                "commit_a": {"type": "string"},
                "commit_b": {"type": "string"},
            },
            "required": ["commit_a", "commit_b"],
        },
    )
    async def diff_artifact(args):
        return _wrap(
            await diff_artifact_impl(ctx, commit_a=args["commit_a"], commit_b=args["commit_b"])
        )

    @tool(
        "get_call_details",
        "Return the snapshot of the linked call's body + metadata "
        "(captured at workspace creation, refreshed by Phase 0). "
        "Returns {linked: false} if no call linked.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def get_call_details(args):
        return _wrap(await get_call_details_impl(ctx))

    @tool(
        "get_profile",
        "Return the snapshot of the linked profile description "
        "(captured at workspace creation). Returns {linked: false} "
        "if no profile linked.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def get_profile(args):
        return _wrap(await get_profile_impl(ctx))

    return [
        read_proposal,
        edit_proposal,
        replace_proposal,
        get_artifact_history,
        read_artifact_at,
        diff_artifact,
        get_call_details,
        get_profile,
    ]
