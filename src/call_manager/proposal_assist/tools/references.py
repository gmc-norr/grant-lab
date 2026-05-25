"""MCP tool implementations for the per-workspace references corpus.

Each *_impl is an async function that takes a ToolContext and returns
a plain dict (registry.py wraps the dict into the MCP envelope). The
six tools added in Slice 2 are read-only — no mutation, no fetch.
"""

from __future__ import annotations

from typing import Any

from call_manager.proposal_assist.tools.registry import ToolContext


async def list_references_impl(
    ctx: ToolContext,
    *,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def search_references_impl(
    ctx: ToolContext,
    *,
    query: str,
    top_k: int = 8,
    kind: str | None = None,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def navigate_reference_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def read_reference_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    max_chars: int = 20_000,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def read_reference_section_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    section_path: str,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def summarize_data_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
) -> dict[str, Any]:
    return {"error": "not implemented"}
