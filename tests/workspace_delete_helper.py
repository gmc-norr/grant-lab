"""Test-only stand-in for the DB half of a delete, with no retirement.

Production deliberately offers only `soft_delete_workspace_db` and
`scrub_workspace_bytes` (Slice C2 §9), and no longer composes them with each
other — Slice A stopped calling scrub_workspace_bytes from the delete path, so
production's delete is `soft_delete_workspace_db` + retirement, full stop. The
old `soft_delete_workspace` wrapper composed the DB and FS halves together and
was the obvious-looking "delete a workspace" entry point — but it skipped
retirement entirely, so any caller silently left passes, jobs and the collab
room running. It is gone from production.

Tests that only need the DB half — because they are asserting DB effects, not
retirement — use this instead. The name says test, so nobody reaches for it in
production by mistake.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from grant_lab.workspaces import soft_delete_workspace_db


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
