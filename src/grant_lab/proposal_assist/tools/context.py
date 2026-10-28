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

_MAX_RUBRIC_DEPTH = 12
"""How deep `_neutralized_criteria` will walk a rubric before truncating.

`rubric_criteria` is member-authored JSON — `web/api/rubric.py`'s `CriterionIn`
sets `extra="allow"` — so an arbitrarily nested structure can be stored, and an
unbounded walk turns `get_call_details` into a RecursionError inside the tool
call. Real rubrics nest two levels (criterion -> subcriteria); 12 is far past
any authored shape.
"""

_RUBRIC_TOO_DEEP = "[rubric nested too deeply to show]"
"""Replacement for a subtree past `_MAX_RUBRIC_DEPTH`. The bound fails CLOSED:
past the cap the structure is replaced by this constant, never passed through
un-neutralised."""


def _neutralized_criteria(criteria: Any, _depth: int = 0) -> Any:
    """Neutralise trust delimiters in EVERY string the rubric carries, however
    deeply nested.

    Neutralised rather than wrapped: the criteria are a structure the agent
    reads field by field, and wrapping each string would bury the shape. The
    numeric weights are not free text and are passed through untouched.

    Recursive on purpose. The rubric schema nests — ``evaluator/context.py``
    reads ``c.get("subcriteria")``, which is a LIST of dicts — and an earlier
    version touched only each criterion's top-level ``str`` values, so
    everything under ``subcriteria`` went to the model raw while the guard read
    as complete. Dict KEYS are neutralised too; a key is free text the member
    typed, and this output is read by the model, not looked up by name.

    Depth-bounded by `_MAX_RUBRIC_DEPTH`; see that constant for why.
    """
    if isinstance(criteria, str):
        return neutralize_trust_delimiters(criteria)
    if _depth >= _MAX_RUBRIC_DEPTH:
        # Fails closed: a subtree past the bound is replaced, not returned raw.
        return _RUBRIC_TOO_DEEP
    if isinstance(criteria, dict):
        return {
            (neutralize_trust_delimiters(k) if isinstance(k, str) else k): _neutralized_criteria(
                v, _depth + 1
            )
            for k, v in criteria.items()
        }
    if isinstance(criteria, list):
        return [_neutralized_criteria(item, _depth + 1) for item in criteria]
    return criteria


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
