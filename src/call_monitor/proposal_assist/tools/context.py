"""Context-retrieval tools: get_call_details, get_profile.

Both read the workspace's SNAPSHOT fields (call_body_snapshot,
profile_desc_snapshot, rubric_criteria, rubric_funder_notes) — NOT the
live calls / profiles rows. The snapshot is captured at workspace
creation and refreshed by Phase 0; the agent's view is stable for the
drafting work's lifetime, immune to later edits of the source rows.

Per phased spec line 418: read-only context tools return workspace
snapshots, not live rows. See Workspace model fields at
src/call_monitor/db/models.py:484+.

Returns {"linked": False} when the snapshot is empty (no call/profile
was linked at workspace creation).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from call_monitor.db.models import Workspace
from call_monitor.proposal_assist.tools.registry import ToolContext


async def get_call_details_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the workspace's snapshot of the linked call body + rubric
    metadata. Returns {"linked": False} when call_body_snapshot is empty."""
    with ctx.session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == ctx.workspace_id))
        if ws is None or not ws.call_body_snapshot:
            return {"linked": False}
        return {
            "linked": True,
            "call_body": ws.call_body_snapshot,
            "rubric_criteria": ws.rubric_criteria or [],
            "rubric_funder_notes": ws.rubric_funder_notes or "",
        }


async def get_profile_impl(ctx: ToolContext) -> dict[str, Any]:
    """Return the workspace's snapshot of the linked profile description.
    Returns {"linked": False} when profile_desc_snapshot is empty."""
    with ctx.session_factory() as s:
        ws = s.scalar(select(Workspace).where(Workspace.id == ctx.workspace_id))
        if ws is None or not ws.profile_desc_snapshot:
            return {"linked": False}
        return {
            "linked": True,
            "description": ws.profile_desc_snapshot,
        }
