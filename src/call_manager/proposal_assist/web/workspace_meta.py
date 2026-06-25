"""Workspace-settings validation constants + parser (neutral, no templates).

Relocated verbatim from the (now-doomed) Jinja ``workspaces.py`` so the kept
``web/api/settings.py`` does not import from a module slated for deletion (G2).
``_CITATION_STYLES`` is NOT relocated here — it is a re-export of
``citations.bibliography.CITATION_STYLES``; importers should use that canonical
name directly.
"""

from __future__ import annotations

from fastapi import HTTPException

# Valid Workspace.status values (mirrors the ck_workspaces_status CHECK
# constraint in db/models.py). Used to allowlist the ?status filter.
_WORKSPACE_STATUSES = frozenset({"drafting", "submitted", "awarded", "rejected", "abandoned"})
# Upper bound on a workspace title — bounds the form field so a multi-MB
# title can't be buffered into the DB. Generous for human-entered titles.
_MAX_WORKSPACE_TITLE_LEN = 500


def _parse_deadline_form(deadline: str | None):
    """Parse an optional ISO-8601 deadline from a form field.

    Returns a datetime or None. Raises HTTPException(400) on a malformed
    string instead of letting datetime.fromisoformat's ValueError bubble up
    as an unhandled 500 (the global handler only catches HTTPException).
    """
    from datetime import datetime

    if not deadline:
        return None
    try:
        return datetime.fromisoformat(deadline)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid deadline (expected ISO-8601)") from e
