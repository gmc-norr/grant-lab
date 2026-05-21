"""Atomically enqueue a user turn for an agent workspace.

This is the entry point B6's POST /workspaces/<id>/turns will call. It
writes one chat_turns row (role='user'), one turn_jobs row
(status='queued', submitted_by_user_id=...), and one workspace_events
audit row (kind='cloud_turn_sent', phase='queued') in the caller's
session/transaction. Caller commits.

The audit row is written BEFORE dispatch so a crash between submit and
finalize doesn't lose the compliance trail. The processor writes a
second event (cloud_turn_completed or cloud_turn_failed) with telemetry
once the turn finishes; the cancel route writes cloud_turn_cancelled
if the user aborts a still-queued turn.

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

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
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
    tokens_in: int | None = None,
) -> TurnJob:
    """Insert chat_turns(role='user') + turn_jobs(status='queued') +
    workspace_events(cloud_turn_sent, phase='queued') atomically.
    Returns the TurnJob (not yet committed).

    `tokens_in`: pre-counted input tokens for this user message (e.g.
    from attachment byte-size estimation in B7). When provided, the value
    is stored on the chat_turns row immediately so the cap query sees it
    as soon as this transaction commits — meaning a concurrent second
    submit that acquires the workspace row lock after this one commits
    will read the updated sum and be gated correctly. Without tokens_in,
    user-turn rows start with tokens_in=NULL and the processor fills in
    the real value from the LLM response later.

    Concurrency: takes a `SELECT ... FOR UPDATE` row lock on the
    workspaces row before reading today's token sums. The lock
    serializes per-workspace submits and is transaction-scoped
    (released on commit/rollback) — orthogonal to the session-scoped
    advisory lock that the drafting subprocess holds.

    The lock only changes observable cap behavior when the cap-query
    input changes between commits. Today the cap query sums
    ChatTurn.tokens_in, which the processor fills in for assistant
    turns AFTER the LLM responds — so two concurrent text-only
    submits both see the same pre-turn sum and the lock is
    defense-in-depth (the cap gate is reactive, not predictive).
    B7 will pre-count attachment tokens at submit time (via the
    `tokens_in` parameter above), and at that point the lock
    genuinely prevents two submits from both passing a cap they
    should not both pass.
    """
    # Lock the workspace row for the duration of this transaction.
    # If the workspace doesn't exist, raise — the caller (web layer)
    # will have already 404'd before reaching this code, so a missing
    # row here means a programmer error.
    locked = session.execute(
        select(Workspace.id)
        .where(Workspace.id == workspace_id)
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise ValueError(f"workspace {workspace_id} does not exist")

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
        tokens_in=tokens_in,
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
