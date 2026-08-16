"""Test-only composition of the two soft-delete halves.

Production deliberately offers only `soft_delete_workspace_db` and
`scrub_workspace_bytes` (Slice C2 §9). The old `soft_delete_workspace` wrapper
composed them and was the obvious-looking "delete a workspace" entry point — but
it skipped retirement entirely, so any caller silently left passes, jobs and the
collab room running. It is gone from production.

Tests that only need "DB half then FS half" — because they are asserting DB
effects, not retirement — use this instead. The name says test, so nobody
reaches for it in production by mistake.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from call_manager.workspaces import soft_delete_workspace_db


def soft_delete_workspace_for_test(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    timeout_seconds: int = 10,
) -> None:
    """The DB half of a delete, with no retirement — what production does minus
    the in-process teardown.

    Slice A: this no longer scrubs bytes, because production no longer does. A
    test that specifically wants byte removal should call `scrub_workspace_bytes`
    directly and say why in a comment.
    """
    soft_delete_workspace_db(
        session, workspace_id=workspace_id, timeout_seconds=timeout_seconds
    )
