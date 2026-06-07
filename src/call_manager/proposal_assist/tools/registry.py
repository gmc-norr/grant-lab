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
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpSdkServerConfig
    from sqlalchemy.orm import sessionmaker

    from call_manager.config import Settings
    from call_manager.proposal_assist.citations import CitationLLMService

logger = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Per-workspace binding passed to every tool handler."""

    workspace_id: uuid.UUID
    repo_dir: Path
    session_factory: sessionmaker
    settings: Settings
    # Phase 3 metadata extraction; None disables LLM extraction gracefully.
    citation_service: CitationLLMService | None = field(default=None)


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


async def _safe_tool(
    tool_name: str,
    ctx: ToolContext,
    impl: Callable[..., Awaitable[dict[str, Any]]],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an *_impl and wrap its result, catching UNEXPECTED exceptions.

    Defense in depth (Slice 2 Task 9, Part C): the *_impl functions are
    contracted to return {"error": ...} for every EXPECTED failure mode and
    never raise to the caller. If one nevertheless raises an unmapped
    exception, it would otherwise propagate out of the MCP handler. We catch
    it here, log it (type + tool + workspace, with a stack trace via
    logger.exception), and return a GENERIC error envelope so no secret /
    quote / reference text reaches the user-facing message.
    """
    try:
        return _wrap(await impl(*args, **kwargs))
    except Exception:  # noqa: BLE001 — catch-all is the whole point here
        logger.exception(
            "tool %s raised an unexpected exception for workspace %s",
            tool_name,
            ctx.workspace_id,
        )
        return _mcp_error("internal error")


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
        return await _safe_tool("read_proposal", ctx, read_proposal_impl, ctx)

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
        return await _safe_tool(
            "edit_proposal",
            ctx,
            edit_proposal_impl,
            ctx,
            edits=args["edits"],
            triggering_turn_id=UUID(ttid) if ttid else None,
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
        return await _safe_tool(
            "replace_proposal",
            ctx,
            replace_proposal_impl,
            ctx,
            content=args["content"],
            triggering_turn_id=UUID(ttid) if ttid else None,
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
        return await _safe_tool(
            "get_artifact_history",
            ctx,
            get_artifact_history_impl,
            ctx,
            limit=int(args.get("limit", 20)),
        )

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
        return await _safe_tool(
            "read_artifact_at",
            ctx,
            read_artifact_at_impl,
            ctx,
            commit_sha=args["commit_sha"],
        )

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
        return await _safe_tool(
            "diff_artifact",
            ctx,
            diff_artifact_impl,
            ctx,
            commit_a=args["commit_a"],
            commit_b=args["commit_b"],
        )

    @tool(
        "get_call_details",
        "Return the snapshot of the linked call's body + metadata "
        "(captured at workspace creation, refreshed by Phase 0). "
        "Returns {linked: false} if no call linked.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def get_call_details(args):
        return await _safe_tool("get_call_details", ctx, get_call_details_impl, ctx)

    @tool(
        "get_profile",
        "Return the snapshot of the linked profile description "
        "(captured at workspace creation). Returns {linked: false} "
        "if no profile linked.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def get_profile(args):
        return await _safe_tool("get_profile", ctx, get_profile_impl, ctx)

    from call_manager.proposal_assist.tools.references import (
        fetch_reference_impl,
        list_references_impl,
        navigate_reference_impl,
        read_reference_impl,
        read_reference_section_impl,
        search_references_impl,
        summarize_data_impl,
    )

    @tool(
        "list_references",
        "List references in this workspace. Filter by kind "
        "(paper/prior_proposal/analysis/data) or substring match on title. "
        "Returns up to `limit` rows with metadata (id, kind, title, status). "
        "Token-cheap — use at the start of a turn to orient.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "q": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": [],
        },
    )
    async def list_references(args):
        return await _safe_tool(
            "list_references",
            ctx,
            list_references_impl,
            ctx,
            kind=args.get("kind"),
            q=args.get("q"),
            limit=int(args.get("limit", 50)),
        )

    @tool(
        "search_references",
        "Semantic search over reference chunks. Embeds the query and "
        "returns the top-k most relevant chunks, each tagged with the "
        "source reference and section_path. Use when you don't know "
        "which reference contains the relevant material.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 8},
                "kind": {"type": "string"},
            },
            "required": ["query"],
        },
    )
    async def search_references(args):
        return await _safe_tool(
            "search_references",
            ctx,
            search_references_impl,
            ctx,
            query=args["query"],
            top_k=int(args.get("top_k", 8)),
            kind=args.get("kind"),
        )

    @tool(
        "navigate_reference",
        "Return the chunk-derived outline of a reference (the hierarchy "
        "of section_path values). Use to discover what sections a "
        "reference has before reading any of them.",
        {
            "type": "object",
            "properties": {"reference_id": {"type": "string"}},
            "required": ["reference_id"],
        },
    )
    async def navigate_reference(args):
        return await _safe_tool(
            "navigate_reference",
            ctx,
            navigate_reference_impl,
            ctx,
            reference_id=args["reference_id"],
        )

    @tool(
        "read_reference",
        "Return the full extracted text of a reference, truncated at "
        "`max_chars` chars. Use when you want the whole content of a "
        "short reference; use read_reference_section for targeted reads "
        "of long references.",
        {
            "type": "object",
            "properties": {
                "reference_id": {"type": "string"},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["reference_id"],
        },
    )
    async def read_reference(args):
        return await _safe_tool(
            "read_reference",
            ctx,
            read_reference_impl,
            ctx,
            reference_id=args["reference_id"],
            max_chars=int(args.get("max_chars", 20000)),
        )

    @tool(
        "read_reference_section",
        "Return concatenated text of all chunks under a given "
        "section_path of a reference. The path is the breadcrumb "
        "navigate_reference returned (e.g. 'Methods > Statistical analysis').",
        {
            "type": "object",
            "properties": {
                "reference_id": {"type": "string"},
                "section_path": {"type": "string"},
            },
            "required": ["reference_id", "section_path"],
        },
    )
    async def read_reference_section(args):
        return await _safe_tool(
            "read_reference_section",
            ctx,
            read_reference_section_impl,
            ctx,
            reference_id=args["reference_id"],
            section_path=args["section_path"],
        )

    @tool(
        "summarize_data",
        "Return schema + sample (first 5 rows) for a kind='data' "
        "reference (CSV/XLSX/ipynb). Use this instead of read_reference "
        "for data files — the model never needs the raw rows.",
        {
            "type": "object",
            "properties": {"reference_id": {"type": "string"}},
            "required": ["reference_id"],
        },
    )
    async def summarize_data(args):
        return await _safe_tool(
            "summarize_data",
            ctx,
            summarize_data_impl,
            ctx,
            reference_id=args["reference_id"],
        )

    @tool(
        "fetch_reference",
        "Fetch a reference from DOI, PMID, arXiv ID, or URL into this "
        "workspace's corpus. Capped at 3 fetches per turn — if you hit "
        "the cap, ask the user to upload remaining refs via the UI. "
        "Returns the new reference_id once the file is on disk and "
        "indexed (the agent can search_references for it immediately).",
        {
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "kind": {"type": "string", "default": "paper"},
            },
            "required": ["input"],
        },
    )
    async def fetch_reference(args):
        return await _safe_tool(
            "fetch_reference",
            ctx,
            fetch_reference_impl,
            ctx,
            input=args["input"],
            kind=args.get("kind", "paper"),
        )

    from call_manager.proposal_assist.tools.citations import (
        insert_citation_impl,
        list_citations_impl,
        remove_citation_impl,
        rename_citekey_impl,
        update_citation_impl,
    )

    @tool(
        "list_citations",
        "List every existing citation in this proposal, read-only. Returns each "
        "citation's `ev_id`, `citekey`, the cited reference/chunk "
        "(reference_id, chunk_index, section_path), stance, confidence, the "
        "claim text the marker attaches to, and the evidence quote. Call this to "
        "discover the `ev-...` evidence id required by remove_citation and "
        "update_citation, and the citekey required by rename_citekey. "
        "Token-cheap — use it before mutating a citation.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_citations(args):
        async def _impl(ctx):
            return list_citations_impl(ctx)

        return await _safe_tool("list_citations", ctx, _impl, ctx)

    @tool(
        "insert_citation",
        "Insert an inline citation marker into proposal.md right after "
        "`anchor` and record the supporting evidence in the citation ledger, "
        "in one atomic commit. You MUST pass an exact `chunk_index` and a "
        "VERBATIM `evidence_quote` copied from that chunk — search_references "
        "and read_reference_section surface the chunk_index to use. The quote "
        "is verified against the chunk text; a quote that is absent or "
        "ambiguous in the chunk is rejected with no commit. `anchor` is a "
        "unique substring of proposal.md after which the marker is placed.",
        {
            "type": "object",
            "properties": {
                "reference_id": {"type": "string"},
                "anchor": {"type": "string"},
                "chunk_index": {"type": "integer"},
                "evidence_quote": {"type": "string"},
                "stance": {
                    "type": "string",
                    "enum": ["supports", "contradicts", "background"],
                    "default": "supports",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
            },
            "required": ["reference_id", "anchor", "chunk_index", "evidence_quote"],
        },
    )
    async def insert_citation(args):
        try:
            chunk_index = int(args["chunk_index"])
        except (TypeError, ValueError, KeyError):
            return _mcp_error("chunk_index must be an integer")
        return await _safe_tool(
            "insert_citation",
            ctx,
            insert_citation_impl,
            ctx,
            reference_id=args["reference_id"],
            anchor=args["anchor"],
            chunk_index=chunk_index,
            evidence_quote=args["evidence_quote"],
            stance=args.get("stance", "supports"),
            confidence=args.get("confidence"),
        )

    @tool(
        "remove_citation",
        "Remove one citation occurrence: its inline marker in proposal.md "
        "AND its row in the citation ledger, in one atomic commit. Pass the "
        "`ev-...` evidence id of the occurrence to remove (call list_citations "
        "to discover it). Other markers/rows that share the same citekey are "
        "left untouched.",
        {
            "type": "object",
            "properties": {"evidence_id": {"type": "string"}},
            "required": ["evidence_id"],
        },
    )
    async def remove_citation(args):
        return await _safe_tool(
            "remove_citation",
            ctx,
            remove_citation_impl,
            ctx,
            evidence_id=args["evidence_id"],
        )

    @tool(
        "update_citation",
        "Update an existing citation's binding or metadata (identified by its "
        "`ev-...` evidence id, which list_citations surfaces). When "
        "`chunk_index` or `evidence_quote` change, "
        "the new quote is re-verified against the chunk text and rejected if "
        "absent or ambiguous. Set `reaccept_claim=true` to re-snapshot the "
        "live claim sentence around the marker (use after editing the prose "
        "near a citation). Specify at least one field to change.",
        {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "chunk_index": {"type": "integer"},
                "evidence_quote": {"type": "string"},
                "stance": {
                    "type": "string",
                    "enum": ["supports", "contradicts", "background"],
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
                "reaccept_claim": {"type": "boolean", "default": False},
            },
            "required": ["evidence_id"],
        },
    )
    async def update_citation(args):
        raw = args.get("chunk_index")
        if raw is not None:
            try:
                raw = int(raw)
            except (TypeError, ValueError):
                return _mcp_error("chunk_index must be an integer")
        return await _safe_tool(
            "update_citation",
            ctx,
            update_citation_impl,
            ctx,
            evidence_id=args["evidence_id"],
            chunk_index=raw,
            evidence_quote=args.get("evidence_quote"),
            stance=args.get("stance"),
            confidence=args.get("confidence"),
            reaccept_claim=bool(args.get("reaccept_claim", False)),
        )

    @tool(
        "rename_citekey",
        "Rename a citekey across ALL of its inline markers in proposal.md and "
        "all of its rows in the citation ledger, in one atomic commit "
        "(evidence ids preserved). The `old_citekey` can be obtained from "
        "list_citations. Rejected if `new_citekey` is already in use in this "
        "workspace.",
        {
            "type": "object",
            "properties": {
                "old_citekey": {"type": "string"},
                "new_citekey": {"type": "string"},
            },
            "required": ["old_citekey", "new_citekey"],
        },
    )
    async def rename_citekey(args):
        return await _safe_tool(
            "rename_citekey",
            ctx,
            rename_citekey_impl,
            ctx,
            old_citekey=args["old_citekey"],
            new_citekey=args["new_citekey"],
        )

    return [
        # existing eight
        read_proposal,
        edit_proposal,
        replace_proposal,
        get_artifact_history,
        read_artifact_at,
        diff_artifact,
        get_call_details,
        get_profile,
        # six new (Slice 2)
        list_references,
        search_references,
        navigate_reference,
        read_reference,
        read_reference_section,
        summarize_data,
        # Slice 3 — agent-side fetch
        fetch_reference,
        # Slice 2 Task 9 — citation mutation tools
        insert_citation,
        remove_citation,
        update_citation,
        rename_citekey,
        # read-only citation discovery
        list_citations,
    ]
