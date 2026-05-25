"""MCP tool implementations for the per-workspace references corpus.

Each *_impl is an async function that takes a ToolContext and returns
a plain dict (registry.py wraps the dict into the MCP envelope). The
six tools added in Slice 2 are read-only — no mutation, no fetch.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.registry import ToolContext


def _load_workspace_reference(ctx: ToolContext, reference_id: str) -> Reference | None:
    """Resolve a Reference by ID, scoped to ctx.workspace_id. Returns None
    on any mismatch (unknown id OR cross-workspace IDOR attempt)."""
    try:
        rid = uuid.UUID(reference_id)
    except ValueError:
        return None
    with ctx.session_factory() as s:
        ref = s.get(Reference, rid)
    if ref is None or ref.workspace_id != ctx.workspace_id:
        return None
    return ref


def _text_path_for(ctx: ToolContext, ref: Reference) -> Path:
    storage = ReferenceStorage(root=Path(ctx.settings.workspaces_dir))
    return storage.text_path_for(workspace_id=ref.workspace_id, reference_id=ref.id)


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
    ref = _load_workspace_reference(ctx, reference_id)
    if ref is None:
        return {"error": f"reference {reference_id} not found"}
    if ref.text_status != "done":
        return {
            "error": (
                f"reference text status is {ref.text_status!r}; "
                f"cannot read full text"
            ),
            "text_status": ref.text_status,
            "text_error": ref.text_error,
        }
    text_path = _text_path_for(ctx, ref)
    if not text_path.exists():
        return {"error": "extracted text missing on disk"}
    full = text_path.read_text(encoding="utf-8")
    truncated = len(full) > max_chars
    return {
        "text": full[:max_chars],
        "truncated": truncated,
        "total_chars": len(full),
    }


async def read_reference_section_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    section_path: str,
) -> dict[str, Any]:
    """Return concatenated chunks under a section_path. Matches prefix
    so requesting 'Methods' returns 'Methods' + 'Methods > Stats' +
    'Methods > Cohort'."""
    ref = _load_workspace_reference(ctx, reference_id)
    if ref is None:
        return {"error": f"reference {reference_id} not found"}
    if ref.text_status != "done":
        return {"error": f"reference text status is {ref.text_status!r}"}
    with ctx.session_factory() as s:
        stmt = (
            select(ReferenceChunk)
            .where(
                ReferenceChunk.reference_id == ref.id,
                or_(
                    ReferenceChunk.section_path == section_path,
                    ReferenceChunk.section_path.like(f"{section_path} > %"),
                ),
            )
            .order_by(ReferenceChunk.chunk_index)
        )
        chunks = list(s.scalars(stmt))
    if not chunks:
        return {"error": f"no chunks found for section_path {section_path!r}"}
    return {
        "section_path": section_path,
        "text": "\n\n".join(c.text for c in chunks),
        "chunk_count": len(chunks),
    }


async def summarize_data_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
) -> dict[str, Any]:
    return {"error": "not implemented"}
