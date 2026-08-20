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

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from call_manager.proposal_assist.ports import Ports
from call_manager.proposal_assist.ports.invocation import Invocation as Invocation

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
    # Serializes the read-modify-write of proposal.md + citations.json across
    # concurrent tool calls WITHIN a single drafting turn. The MCP server and all
    # its tool closures are built once per workspace launch from one ToolContext,
    # so this single lock covers every citation mutation in that turn. It does NOT
    # provide cross-process exclusivity — the controller's Postgres advisory lock,
    # the turn_jobs running-unique index, and begin_in_flight_exclusive already
    # guarantee only one turn/subprocess per workspace runs at a time.
    #
    # default_factory=asyncio.Lock is safe to construct outside a running loop in
    # Python 3.12: the Lock binds to the running loop lazily on first use.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Spec 2026-06-10 §1: the agent's view of HEAD. Updated by read_proposal
    # and by each successful mutation (tools return the new commit SHA).
    # edit/replace refuse to write when the captured expected head differs
    # from actual HEAD inside the commit mutex — a blind overwrite of text
    # the agent never saw becomes a tool error instead of a lost update.
    agent_view_head: str | None = field(default=None)

    # Process-wide ingest admission limiter (threading.Semaphore-backed).
    # None disables the limiter guard in fetch_reference_impl (e.g. in tests
    # that build a bare ToolContext without app.state).
    reference_ingest_limiter: object | None = field(default=None)

    # SP2a-2 §6: per-turn provenance, stamped server-side by process_turn_once
    # via DraftingAgent.set_turn_provenance onto the retained ToolContext before
    # each send_turn. NEVER set from model tool args (those are spoofable). The
    # propose_* tools copy these onto the proposed_change row. Stamped
    # UNCONDITIONALLY every turn because the subprocess (and this ctx) is reused
    # across turns — a missed stamp would leak the prior turn's provenance.
    triggering_turn_id: uuid.UUID | None = field(default=None)
    triggering_prompt: str | None = field(default=None)
    created_by: int | None = field(default=None)
    origin_session_id: str | None = field(default=None)

    # SP2b-2c: provenance for non-interactive (background) origins. Defaults keep
    # the interactive path stamping "interactive_turn" with no origin_job_id. The
    # coherence-keeper's tools_factory sets origin_kind="coherence_session" +
    # origin_job_id=<coherence_jobs.id> at factory time (NOT via set_turn_provenance).
    origin_kind: str = field(default="interactive_turn")
    origin_job_id: uuid.UUID | None = field(default=None)

    # F3a: the generation this actor CLAIMED at — its belief for the whole run.
    # One-shot contexts (the four background agents) receive it at construction.
    # The retained interactive context cannot: it is built during
    # controller.launch(), which deliberately precedes the claim
    # (turns/processor.py — the orphan sweep must run first), so the value does
    # not exist yet. It is stamped per turn by set_turn_provenance instead, and
    # snapshotted per INVOCATION by the dispatcher (see Invocation).
    lifecycle_generation: int | None = field(default=None)
    # F3a: the turn job that owns THIS turn's tool calls, for owner-bound
    # accounting. NOT origin_job_id — that is deliberately None for interactive
    # contexts (see the origin_kind comment above) and is copied into durable
    # provenance by the propose_* tools, so reusing it would change stored data.
    active_turn_job_id: uuid.UUID | None = field(default=None)

    # SP2b-3a: the evaluator pass pins the section counts of the artifacts it
    # assembled into the instruction (captured at run start), so submit_scorecard
    # validates deep_link.section_index against the SAME HEAD it evaluated.
    eval_draft_section_count: int | None = field(default=None)
    eval_spec_section_count: int | None = field(default=None)
    # SP2b-3a: the rubric criteria snapshot the evaluator was SHOWN at run start,
    # pinned so submit_scorecard validates/weights against exactly what the model
    # judged — never a live workspace rubric edited mid-run.
    eval_rubric_criteria: list[dict] | None = field(default=None)

    # SP2b-3c: the mock-panel sub-call's target ReviewRunItem. submit_review writes
    # the produced markdown onto this row. origin_kind="review_session",
    # origin_job_id=<review_runs.id>.
    review_run_item_id: uuid.UUID | None = field(default=None)

    # SP2b-3d: the uncited-claims pass pins the draft section count it scanned, so
    # submit_uncited_claims validates each claim's section_index against the SAME draft.
    uncited_draft_section_count: int | None = field(default=None)

    # SP2a-2 §3 (Task 2): section_index -> section sha as the agent saw them at
    # read time (populated by read_proposal_impl, HEAD-first). propose_edit reads
    # base_section_sha from here; an unread section is rejected (_NOT_READ_ERROR).
    agent_view_sections: dict[int, str] = field(default_factory=dict)

    # SP2a-2 §3 (Task 2): the full proposal text the agent saw at read time
    # (populated by read_proposal_impl alongside agent_view_sections). propose_
    # citation (Task 5) walks split_sections(agent_view_proposal) to map an
    # anchor offset -> section_index from the SAME read-time snapshot it took the
    # section shas from, so the (section_index, base_section_sha) pair stays
    # internally consistent even if a human added/removed a `##` section between
    # the read and the propose. None until the agent has read once.
    agent_view_proposal: str | None = field(default=None)

    # SP2b-2b: the agent's read-time view of the SPEC artifact (parallel to the
    # draft fields above). Populated by read_spec_impl; propose_spec_edit_impl
    # reads its base from here (anti-clobber, read-time not live).
    agent_view_spec_head: str | None = field(default=None)
    agent_view_spec_sections: dict[int, str] = field(default_factory=dict)
    agent_view_spec_proposal: str | None = field(default=None)

    # Selection-actions (Task 4): per-turn runtime tool gate. None = normal chat
    # (full launch-time superset applies). Set = only those tools are allowed for
    # the current turn. Stamped by the controller before each send_turn when the
    # turn content contains a selection block.
    allowed_tool_names: set[str] | None = field(default=None)
    turn_selection: dict | None = field(default=None)

    # F3d1: the capability bundle a converted tool holds INSTEAD of a session.
    # Defaults to None because two constructions deliberately have no session to
    # build one from — collect_mcp_tool_names below and proposals/accepting.py's
    # shim, both of which pass session_factory=None. tests/fencing/detectors.py
    # keys on that literal to classify them inert, so they must stay as they are.
    ports: Ports | None = field(default=None)


def snapshot_invocation(ctx: ToolContext) -> Invocation:
    """Freeze one tool call's identity, at DISPATCH.

    Lives here rather than on Invocation because it reads a ToolContext and ports/
    is stdlib-only. Called before any await or thread handoff: ToolContext is
    MUTABLE and shared, set_turn_provenance restamps it every turn, so a tool
    reading these values when its effect happens can read a LATER turn's.
    """
    return Invocation(
        workspace_id=ctx.workspace_id,
        lifecycle_generation=ctx.lifecycle_generation,
        active_turn_job_id=ctx.active_turn_job_id,
        origin_kind=ctx.origin_kind,
        origin_job_id=ctx.origin_job_id,
        triggering_turn_id=ctx.triggering_turn_id,
        triggering_prompt=ctx.triggering_prompt,
        created_by=ctx.created_by,
        origin_session_id=ctx.origin_session_id,
        turn_selection=Invocation._freeze(ctx.turn_selection),
    )


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


def collect_mcp_tool_names() -> frozenset[str]:
    """Return the canonical frozenset of ALL tool names the shared MCP server
    exposes, derived from a minimal ToolContext.

    Used by ClaudeAgentSDKAdapter to compute the hard-deny disallowed_tools
    set for background sub-agents (evaluator / review / uncited-claims). The
    set is stable across calls — tool registration is deterministic.
    """
    import pathlib
    import uuid as _uuid

    ctx = ToolContext(
        workspace_id=_uuid.UUID(int=0),
        repo_dir=pathlib.Path("/tmp"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    return frozenset(t.name for t in _collect_tools(ctx))


def _wrap(impl_result: dict[str, Any]) -> dict[str, Any]:
    """Translate an *_impl plain-dict return into the MCP envelope."""
    if "error" in impl_result:
        return _mcp_error(impl_result["error"])
    return _mcp_ok(impl_result)


def _deny_if_out_of_turn(tool_name: str, ctx: ToolContext) -> dict[str, Any] | None:
    """Per-turn runtime gate. Returns None when allowed (no per-turn list set, or
    the tool is in it) and an _mcp_error envelope when denied. The launched client
    keeps its full tool SUPERSET across turns, so this is the authoritative
    per-turn deny."""
    if ctx.allowed_tool_names is not None and tool_name not in ctx.allowed_tool_names:
        return _mcp_error(f"tool '{tool_name}' is not allowed for this turn")
    return None


async def _safe_tool(
    tool_name: str,
    ctx: ToolContext,
    impl: Callable[..., Awaitable[dict[str, Any]]],
    *args: Any,
    pass_invocation: bool = False,
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

    F3a Task 6: when pass_invocation=True, a frozen Invocation snapshot is
    taken at DISPATCH (before any await or thread handoff, and before the
    deny check so a denied call cannot leave a half-built identity anywhere)
    and passed to the impl as the keyword argument ``invocation``. No
    production call site sets pass_invocation=True in F3a; F3d flips it
    per tool as effect sites are converted.
    """
    # Snapshot BEFORE the deny check: a denied call must not leave a
    # half-built identity anywhere (functionally moot in F3a, but the
    # ordering is the contract).
    if pass_invocation:
        kwargs["invocation"] = snapshot_invocation(ctx)
    denied = _deny_if_out_of_turn(tool_name, ctx)
    if denied is not None:
        return denied
    try:
        result = await impl(*args, **kwargs)
        return _wrap(result)
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
        propose_edit_impl,
        propose_spec_edit_impl,
        read_proposal_impl,
        read_spec_impl,
    )

    @tool(
        "read_proposal",
        "Return the current proposal.md content.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def read_proposal(args):
        return await _safe_tool("read_proposal", ctx, read_proposal_impl, ctx)

    @tool(
        "propose_edit",
        "Propose a full-text replacement of one section of proposal.md as a "
        "PENDING change for a human to review and accept — this does NOT edit "
        "or commit anything. `section_index` is the 0-based section you are "
        "rewriting (0 is the preamble); call read_proposal first so the system "
        "knows which version you based your edit on. `new_text` is the WHOLE new "
        "section including its `## heading` line. Returns a proposed_change_id "
        "and status 'pending'.",
        {
            "type": "object",
            "properties": {
                "section_index": {"type": "integer"},
                "new_text": {"type": "string"},
            },
            "required": ["section_index", "new_text"],
        },
    )
    async def propose_edit(args):
        denied = _deny_if_out_of_turn("propose_edit", ctx)
        if denied is not None:
            return denied
        try:
            section_index = int(args["section_index"])
        except (TypeError, ValueError, KeyError):
            return _mcp_error("section_index must be an integer")
        return await _safe_tool(
            "propose_edit",
            ctx,
            propose_edit_impl,
            ctx,
            section_index=section_index,
            new_text=args["new_text"],
        )

    @tool(
        "read_spec",
        "Return the current proposal-spec.md (the Spec artifact) content. Call this "
        "before propose_spec_edit so the system knows which version you based your "
        "edit on.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def read_spec(args):
        return await _safe_tool("read_spec", ctx, read_spec_impl, ctx)

    @tool(
        "propose_spec_edit",
        "Propose a full-text replacement of one section of the SPEC (proposal-spec.md) "
        "as a PENDING change for a human to review and accept — this does NOT edit or "
        "commit anything. `section_index` is the 0-based section (0 is the preamble); "
        "call read_spec first. `new_text` is the WHOLE new section including its "
        "`## heading` line. Returns a proposed_change_id and status 'pending'.",
        {
            "type": "object",
            "properties": {
                "section_index": {"type": "integer"},
                "new_text": {"type": "string"},
            },
            "required": ["section_index", "new_text"],
        },
    )
    async def propose_spec_edit(args):
        try:
            section_index = int(args["section_index"])
        except (TypeError, ValueError, KeyError):
            return _mcp_error("section_index must be an integer")
        return await _safe_tool(
            "propose_spec_edit",
            ctx,
            propose_spec_edit_impl,
            ctx,
            section_index=section_index,
            new_text=args["new_text"],
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
        list_citations_impl,
        propose_citation_impl,
    )

    @tool(
        "list_citations",
        "List existing citations in this proposal, read-only. Returns each "
        "citation's `ev_id`, `citekey`, the cited reference/chunk "
        "(reference_id, chunk_index, section_path), stance, confidence, the "
        "claim text the marker attaches to, and the evidence quote, plus a "
        "`truncated` flag. Returns up to `limit` rows (default 100, max 500). "
        "Call this before proposing a citation change via propose_citation. "
        "Token-cheap — use it at the start of citation work.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
            },
            "required": [],
        },
    )
    async def list_citations(args):
        # Pass the raw limit through: list_citations_impl coerces it inside its
        # own try/except, so a non-coercible value returns a tool error envelope
        # via _safe_tool rather than raising int(...) OUTSIDE the crash-guard.
        return await _safe_tool(
            "list_citations",
            ctx,
            list_citations_impl,
            ctx,
            limit=args.get("limit", 100),
        )

    @tool(
        "propose_citation",
        "Propose attaching an inline citation as a PENDING change for a human to "
        "review and accept — this does NOT insert or commit anything. Pass the "
        "`reference_id`, an `anchor` (a unique substring of the draft the marker "
        "should follow), the `chunk_index`, and a VERBATIM `evidence_quote` "
        "copied word-for-word from that chunk (search_references / "
        "read_reference_section surface the chunk_index). The quote and anchor "
        "are verified now; an unverifiable citation is rejected and never "
        "becomes a pending proposal. The citekey + evidence id are assigned when "
        "a human accepts. Call read_proposal first. Returns a proposed_change_id "
        "and status 'pending'.",
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
    async def propose_citation(args):
        denied = _deny_if_out_of_turn("propose_citation", ctx)
        if denied is not None:
            return denied
        try:
            chunk_index = int(args["chunk_index"])
        except (TypeError, ValueError, KeyError):
            return _mcp_error("chunk_index must be an integer")
        return await _safe_tool(
            "propose_citation",
            ctx,
            propose_citation_impl,
            ctx,
            reference_id=args["reference_id"],
            anchor=args["anchor"],
            chunk_index=chunk_index,
            evidence_quote=args["evidence_quote"],
            stance=args.get("stance", "supports"),
            confidence=args.get("confidence"),
        )

    from call_manager.proposal_assist.tools.audit import (
        build_bibliography_impl,
        list_unsupported_claims_impl,
        validate_citations_impl,
    )
    from call_manager.proposal_assist.tools.review_submit import submit_review_impl
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl
    from call_manager.proposal_assist.tools.uncited_claims import submit_uncited_claims_impl

    @tool(
        "validate_citations",
        "Validate every citation in the proposal. Runs deterministic checks "
        "(drift, missing references/chunks, broken quotes, orphaned markers) "
        "and, when available, an LLM semantic judge (does the evidence support "
        "the claim?). Returns a list of issues with "
        "type/severity/citekey/evidence_id/location/detail; fix issues by "
        "proposing a corrected citation via propose_citation. "
        "`semantic_available` is false when the audit LLM is unavailable or "
        "capped (deterministic results still returned).",
        {"type": "object", "properties": {}, "required": []},
    )
    async def validate_citations(args):
        return await _safe_tool("validate_citations", ctx, validate_citations_impl, ctx)

    @tool(
        "list_unsupported_claims",
        "Heuristic list of factual/empirical claims in the proposal that lack a "
        "citation, each with a suggested search_references query. Uses the audit "
        "LLM to judge uncited sentences; not exhaustive. `semantic_available` is "
        "false when the audit LLM is unavailable/capped (then candidates may be "
        "empty/partial). Use search_references on a suggested query, then "
        "propose_citation.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_unsupported_claims(args):
        return await _safe_tool("list_unsupported_claims", ctx, list_unsupported_claims_impl, ctx)

    @tool(
        "build_bibliography",
        "Render the proposal's cited references into a formatted bibliography.md "
        "(a separate file — never merged into proposal.md). Fail-closed: if any "
        "cited reference's metadata is unapproved (and unwaived), unresolved, or "
        "tombstoned, nothing is written and the response is "
        "`{error, blocked:[{citekey, reference_id, metadata_status, reason}]}` — "
        "approve the listed references (or have the owner waive them) and retry. "
        "`style` is optional and defaults to the workspace's citation_style. Call "
        "this once the draft's citations are settled.",
        {
            "type": "object",
            "properties": {
                "style": {"type": "string", "enum": ["vancouver", "author-year"]},
            },
            "required": [],
        },
    )
    async def build_bibliography(args):
        return await _safe_tool(
            "build_bibliography",
            ctx,
            build_bibliography_impl,
            ctx,
            style=args.get("style"),
        )

    @tool(
        "submit_scorecard",
        "Submit the COMPLETE evaluation scorecard exactly once. `criteria` MUST contain "
        "one entry per funder criterion, each with `index` (the 0-based [index] shown in the "
        "criteria list), the matching `name`, a `band` (strong|adequate|at_risk|gap), a one-"
        "paragraph `note`, and an optional `deep_link` {artifact:'draft'|'spec', section_index}. "
        "`summary` is a short overall readiness note. The overall band is computed server-side.",
        {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "name": {"type": "string"},
                            "band": {
                                "type": "string",
                                "enum": ["strong", "adequate", "at_risk", "gap"],
                            },
                            "note": {"type": "string"},
                            "deep_link": {
                                "type": "object",
                                "properties": {
                                    "artifact": {"type": "string", "enum": ["draft", "spec"]},
                                    "section_index": {"type": "integer"},
                                },
                                "required": ["artifact", "section_index"],
                            },
                        },
                        "required": ["index", "name", "band", "note"],
                    },
                },
                "summary": {"type": "string"},
            },
            "required": ["criteria", "summary"],
        },
    )
    async def submit_scorecard(args):
        return await _safe_tool(
            "submit_scorecard",
            ctx,
            submit_scorecard_impl,
            ctx,
            criteria=args.get("criteria") or [],
            summary=args.get("summary", ""),
        )

    @tool(
        "submit_review",
        "Submit your mock-panel review markdown exactly once.",
        {
            "type": "object",
            "properties": {"markdown": {"type": "string"}},
            "required": ["markdown"],
        },
    )
    async def submit_review(args):
        return await _safe_tool(
            "submit_review",
            ctx,
            submit_review_impl,
            ctx,
            markdown=args.get("markdown", ""),
        )

    @tool(
        "submit_uncited_claims",
        "Submit the COMPLETE list of uncited factual claims found in the DRAFT, exactly "
        "once. Each entry needs `claim_text` (the VERBATIM assertion text, copied from the "
        "section so it is locatable), `section_index` (the 0-based [§index] it appears in), "
        "and a one-line `reason` (why it needs a citation). Flag only factual/empirical "
        "assertions lacking an adjacent citation marker; EXCLUDE the applicant's own "
        "proposed work, common knowledge, and already-cited statements. Submit an empty "
        "list if the draft has no uncited claims. You have no other tools.",
        {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim_text": {"type": "string"},
                            "section_index": {"type": "integer"},
                            "reason": {"type": "string"},
                        },
                        "required": ["claim_text", "section_index", "reason"],
                    },
                }
            },
            "required": ["claims"],
        },
    )
    async def submit_uncited_claims(args):
        return await _safe_tool(
            "submit_uncited_claims",
            ctx,
            submit_uncited_claims_impl,
            ctx,
            claims=args.get("claims") or [],
        )

    return [
        read_proposal,
        # SP2a-2 — propose-only mutation
        propose_edit,
        # SP2b-2b — spec artifact (read + propose-only)
        read_spec,
        propose_spec_edit,
        get_artifact_history,
        read_artifact_at,
        diff_artifact,
        get_call_details,
        get_profile,
        # Slice 2 — references
        list_references,
        search_references,
        navigate_reference,
        read_reference,
        read_reference_section,
        summarize_data,
        # Slice 3 — agent-side fetch
        fetch_reference,
        # read-only citation discovery
        list_citations,
        # SP2a-2 — propose-only citation
        propose_citation,
        # Slice 3 — citation audit
        validate_citations,
        # Slice 3 — uncited-claim discovery
        list_unsupported_claims,
        # Slice 4 — bibliography rendering
        build_bibliography,
        # SP2b-3a — evaluator scorecard (the evaluator agent's ONLY tool)
        submit_scorecard,
        # SP2b-3c — mock-panel review (the review sub-call's ONLY tool)
        submit_review,
        # SP2b-3d — uncited-claims (the uncited-claims pass's ONLY tool)
        submit_uncited_claims,
    ]
