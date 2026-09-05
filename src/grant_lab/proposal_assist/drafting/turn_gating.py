"""Per-turn tool allow-lists for selection-driven turns.

The SDK client is launched once with a fixed tool SUPERSET and reused across
turns, so per-turn gating is RUNTIME denial via ToolContext.allowed_tool_names
(checked in tools/registry._safe_tool). These sets are EXPLICIT (not subtractive)
so mutating tools like fetch_reference / build_bibliography are never implicitly
left in. Normal chat (no selection block) returns None -> the launch-time full
list applies.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_proposal", "read_spec", "get_artifact_history", "read_artifact_at",
    "diff_artifact", "get_call_details", "get_profile", "list_references",
    "search_references", "navigate_reference", "read_reference",
    "read_reference_section", "summarize_data", "list_citations",
    "validate_citations", "list_unsupported_claims",
})
MODIFY_TOOLS: frozenset[str] = READ_ONLY_TOOLS | {"propose_edit"}

_ALLOWED_BY_REQUEST: dict[str, frozenset[str]] = {
    "ask": READ_ONLY_TOOLS,
    "modify": MODIFY_TOOLS,
}


def _selection_block(content: Any) -> dict | None:
    blocks = content if isinstance(content, list) else [content]
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "selection":
            return b
    return None


def allowed_tools_for_content(content: Any) -> set[str] | None:
    """Per-turn allow-set, or None for normal chat (full launch-time list)."""
    sel = _selection_block(content)
    if sel is None:
        return None
    rt = sel.get("request_type")
    allowed = _ALLOWED_BY_REQUEST.get(rt)
    if allowed is None:
        # Fail closed: an unrecognized request_type denies ALL tools this turn
        # rather than silently granting the read-only set.
        logger.warning("unknown selection request_type %r — denying all tools this turn", rt)
        return set()
    return set(allowed)


def selection_from_content(content: Any) -> dict | None:
    """The selection block's anchor/offset provenance, or None."""
    sel = _selection_block(content)
    if sel is None:
        return None
    return {
        "anchor": sel.get("anchor"),
        "from_offset": sel.get("from_offset"),
        "to_offset": sel.get("to_offset"),
        "section_index": sel.get("section_index"),
        "verified": sel.get("verified"),
    }
