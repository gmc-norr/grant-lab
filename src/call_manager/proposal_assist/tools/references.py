"""MCP tool implementations for the per-workspace references corpus.

Each *_impl is an async function that takes a ToolContext and returns
a plain dict (registry.py wraps the dict into the MCP envelope). The
six tools added in Slice 2 are read-only — no mutation, no fetch.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from call_manager.db.models import Reference
from call_manager.proposal_assist.tools.registry import ToolContext


async def list_references_impl(
    ctx: ToolContext,
    *,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Browse references in the workspace. Token-cheap metadata only."""
    if limit > 200:
        limit = 200  # hard cap
    with ctx.session_factory() as s:
        stmt = (
            select(Reference)
            .where(Reference.workspace_id == ctx.workspace_id)
            .order_by(Reference.created_at.desc())
            .limit(limit)
        )
        if kind:
            stmt = stmt.where(Reference.kind == kind)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(Reference.title.ilike(like), Reference.filename.ilike(like))
            )
        rows = list(s.scalars(stmt))
    return {
        "references": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "title": r.title,
                "filename": r.filename,
                "text_status": r.text_status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


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
