"""Atomically enqueue a user turn for an agent workspace.

This is the entry point B6's POST /workspaces/<id>/turns will call. It
writes one chat_turns row (role='user'), one turn_jobs row
(status='queued', submitted_by_user_id=...), and one workspace_events
audit row (kind='cloud_turn_sent', phase='queued') in the caller's
session/transaction. Caller commits.

The audit row is written BEFORE dispatch so a crash between submit and
finalize doesn't lose the compliance trail. The processor writes a
second event (cloud_turn_completed or cloud_turn_failed) with telemetry
once the turn finishes.

Cap enforcement: pre-write, calls check_daily_token_cap. If exceeded,
raises TokenCapExceeded — caller (typically B6) catches this and
returns 429 to the client. The session's flushed-but-uncommitted state
is rolled back by the caller (or by the request-scope teardown).

B8 will layer the user-facing "you hit the cap" surfacing on top of
the TokenCapExceeded path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
from call_manager.proposal_assist.turns.caps import (
    TokenCapExceeded,
    check_daily_token_cap,
)


def submit_user_turn(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    content: dict[str, Any] | list[Any] | str,
    submitted_by_user_id: int | None,
    settings: Settings,
) -> TurnJob:
    """Insert chat_turns(role='user') + turn_jobs(status='queued') +
    workspace_events(cloud_turn_sent, phase='queued') atomically.
    Returns the TurnJob (not yet committed)."""
    cap = check_daily_token_cap(session, workspace_id, settings=settings)
    if not cap.allowed:
        raise TokenCapExceeded(
            workspace_id,
            used_in=cap.used_in,
            used_out=cap.used_out,
            cap_in=cap.cap_in,
            cap_out=cap.cap_out,
            reason=cap.reason or "cap_exceeded",
        )

    user_turn = ChatTurn(
        workspace_id=workspace_id,
        role="user",
        content=content,
    )
    session.add(user_turn)
    session.flush()  # so user_turn.id is populated for FK below

    job = TurnJob(
        workspace_id=workspace_id,
        user_turn_id=user_turn.id,
        submitted_by_user_id=submitted_by_user_id,
        status="queued",
        queued_at=datetime.now(UTC),
    )
    session.add(job)
    session.flush()

    # Audit-at-submit: survives a crash between submit and finalize.
    # The processor writes a separate cloud_turn_{completed,failed}
    # event with telemetry when the turn finishes.
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="cloud_turn_sent",
            actor_id=submitted_by_user_id,
            payload={
                "turn_job_id": str(job.id),
                "phase": "queued",
            },
        )
    )
    session.flush()
    return job
