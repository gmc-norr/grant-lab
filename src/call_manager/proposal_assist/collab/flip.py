"""Enable-on-touch flip seam (3b-5d.D4): the ONLY prod caller of enable_collab.

maybe_auto_enable is invoked from the draft/spec read models — the first read
of an artifact (kill-switch off, no pipe tables at HEAD) enables collab for it,
stickily and per-artifact. New workspaces converge on their first cockpit load;
existing ones on their next open. Remove the hold-back once
scripts/collab_table_audit.py reports no affected artifacts."""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.proposal_assist.collab import state

logger = logging.getLogger(__name__)

# A pipe-table-shaped row: a line with >=2 pipes separating cell-ish content
# (covers header, |---| delimiter and body rows, with or without a leading
# pipe). Conservative ON PURPOSE: a false positive only DELAYS enablement
# (the artifact stays on the CM6 editor); a false negative flattens a table
# on the first room round-trip. Fenced code blocks are excluded by the scanner.
_TABLE_ROW = re.compile(r"^\s*\|?[^|\n]*\|[^|\n]*\|")


def has_pipe_tables(md: str) -> bool:
    in_fence = False
    for line in md.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _TABLE_ROW.match(line):
            return True
    return False


def maybe_auto_enable(
    session: Session, *, workspace_id: uuid.UUID, artifact: str, head_md: str
) -> bool:
    """Sticky lazy flip. Returns the artifact's EFFECTIVE enabled state.

    Enables iff: kill-switch off AND not already enabled AND head_md has no
    pipe tables (hold-back — the CommonMark bridge would flatten them).
    COMMITS the enable itself (the read models otherwise never write; the
    session factory is expire_on_commit=False so callers' loaded rows survive).
    The concurrent-first-read insert race resolves via IntegrityError → the
    other request won → report enabled."""
    if state.kill_switch_engaged():
        return False
    if state.is_collab_enabled(session, workspace_id=workspace_id, artifact=artifact):
        return True
    if has_pipe_tables(head_md):
        logger.info(
            "collab.flip_held_back",
            extra={
                "event": "collab_flip_held_back",
                "workspace_id": str(workspace_id),
                "artifact": artifact,
            },
        )
        return False
    state.enable_collab(session, workspace_id=workspace_id, artifact=artifact)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # concurrent first read won the insert — same outcome
    logger.info(
        "collab.flip_enabled",
        extra={
            "event": "collab_flip_enabled",
            "workspace_id": str(workspace_id),
            "artifact": artifact,
        },
    )
    return True
