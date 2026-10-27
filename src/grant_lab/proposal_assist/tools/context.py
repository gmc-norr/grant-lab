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

from grant_lab.proposal_assist.ports.trust import (
    neutralize_trust_delimiters,
    wrap_untrusted_reference,
)


def _neutralized_criteria(criteria: Any) -> Any:
    """Neutralise trust delimiters in the rubric's free-text values.

    Neutralised rather than wrapped: the criteria are a structure the agent
    reads field by field, and wrapping each string would bury the shape. The
    numeric weights are not free text and are passed through untouched.
    """
    if not isinstance(criteria, list):
        return criteria
    out = []
    for item in criteria:
        if isinstance(item, dict):
            out.append(
                {
                    k: (neutralize_trust_delimiters(v) if isinstance(v, str) else v)
                    for k, v in item.items()
                }
            )
        elif isinstance(item, str):
            out.append(neutralize_trust_delimiters(item))
        else:
            out.append(item)
    return out


async def get_call_details_impl(ctx, *, ports) -> dict[str, Any]:
    """Return the workspace's snapshot of the linked call body + rubric metadata.
    Returns {"linked": False} when call_body_snapshot is empty.

    The call body is external text — it arrives from the EU/Vinnova feeds, not
    from a member — so it is handed to the model inside the same
    <untrusted-reference> boundary the system prompt promises, with forged
    delimiters neutralised (review 2026-10-26 M-PA-3, which found this tool
    returning it raw).
    """
    snap = ports.queries.get_workspace_snapshot(ctx.workspace_id)
    if snap is None or not snap.call_body:
        return {"linked": False}
    notes = snap.rubric_funder_notes
    return {
        "linked": True,
        "call_body": wrap_untrusted_reference(snap.call_body),
        "rubric_criteria": _neutralized_criteria(snap.rubric_criteria),
        "rubric_funder_notes": (
            neutralize_trust_delimiters(notes) if isinstance(notes, str) else notes
        ),
    }


async def get_profile_impl(ctx, *, ports) -> dict[str, Any]:
    """Return the workspace's snapshot of the linked profile description.
    Returns {"linked": False} when profile_desc_snapshot is empty."""
    snap = ports.queries.get_workspace_snapshot(ctx.workspace_id)
    if snap is None or not snap.profile_desc:
        return {"linked": False}
    return {"linked": True, "description": snap.profile_desc}
