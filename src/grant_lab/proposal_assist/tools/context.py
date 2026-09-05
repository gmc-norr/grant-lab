"""Context-retrieval tools: get_call_details, get_profile.

Both read the workspace's SNAPSHOT fields (call_body_snapshot,
profile_desc_snapshot, rubric_criteria, rubric_funder_notes) — NOT the
live calls / profiles rows. The snapshot is captured at workspace
creation and refreshed by Phase 0; the agent's view is stable for the
drafting work's lifetime, immune to later edits of the source rows.

Per phased spec line 418: read-only context tools return workspace
snapshots, not live rows. See Workspace model fields at
src/grant_lab/db/models.py:484+.

Returns {"linked": False} when the snapshot is empty (no call/profile
was linked at workspace creation).
"""

from __future__ import annotations

from typing import Any


async def get_call_details_impl(ctx, *, ports) -> dict[str, Any]:
    """Return the workspace's snapshot of the linked call body + rubric metadata.
    Returns {"linked": False} when call_body_snapshot is empty."""
    snap = ports.queries.get_workspace_snapshot(ctx.workspace_id)
    if snap is None or not snap.call_body:
        return {"linked": False}
    return {
        "linked": True,
        "call_body": snap.call_body,
        "rubric_criteria": snap.rubric_criteria,
        "rubric_funder_notes": snap.rubric_funder_notes,
    }


async def get_profile_impl(ctx, *, ports) -> dict[str, Any]:
    """Return the workspace's snapshot of the linked profile description.
    Returns {"linked": False} when profile_desc_snapshot is empty."""
    snap = ports.queries.get_workspace_snapshot(ctx.workspace_id)
    if snap is None or not snap.profile_desc:
        return {"linked": False}
    return {"linked": True, "description": snap.profile_desc}
