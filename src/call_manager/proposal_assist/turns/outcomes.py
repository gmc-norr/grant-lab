"""Centralized terminal-outcome recording for proposal-assist turns.

Every terminal failed/cancelled transition routes through
``record_terminal_outcome`` so the status flip, the viewer-safe ``system``
chat row, and the audit event stay consistent across all five writers
(processor finalize, reaper, orphan sweep, cancel route, soft-delete).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent


def outcome_message(outcome: str, raw_error: str | None) -> str:
    """Map an outcome + raw error to a short, viewer-safe sentence.

    Args:
        outcome: ``"cancelled"`` or ``"failed"``; any other value is treated
            as failed.
        raw_error: internal diagnostics string — never echoed into the returned
            message (it can contain stack/path diagnostics).

    Phrased neutrally: the same row is shown to viewers who cannot act,
    and 'orphaned' can follow either a crash or a redeploy.
    """
    if outcome == "cancelled":
        return "This turn was cancelled before it started."
    if raw_error is not None and raw_error.startswith("reaped:"):
        return "This turn was stopped after running too long."
    if raw_error == "orphaned":
        return "This turn was interrupted. Please try again."
    return "The drafting assistant couldn't finish this turn. Please try again."


def record_terminal_outcome(
    session: Session,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    outcome: str,            # 'failed' | 'cancelled'
    expected_status: str,    # 'running' | 'queued' — the conditional guard
    raw_error: str | None,   # stored in the audit event, NEVER in the chat row
    emit_chat: bool = True,  # False → audit event only (soft-delete: no surface)
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> bool:
    """Record a terminal failed/cancelled outcome in the caller's transaction.

    Returns True if this call performed the transition, False if the job was
    already terminal / in the wrong workspace (no double-finalize). The caller
    commits.
    """
    # Workspace-scoped conditional flip + atomic read-back of the authoritative
    # ids. Test the RETURNING result, NOT rowcount (psycopg3 reports rowcount
    # unreliably for UPDATE ... RETURNING).
    row = session.execute(
        update(TurnJob)
        .where(
            TurnJob.id == turn_job_id,
            TurnJob.status == expected_status,
            TurnJob.workspace_id == workspace_id,
        )
        .values(status=outcome, error=raw_error, completed_at=datetime.now(UTC))
        .returning(TurnJob.user_turn_id, TurnJob.submitted_by_user_id)
    ).first()
    if row is None:
        return False
    user_turn_id, submitted_by_user_id = row
    # Clear the submit-time tokens_in pre-count on the user-turn row so a
    # terminal turn doesn't keep burning daily-cap budget for input it never
    # actually consumed (mirrors the finalize/reaper behaviour).
    if user_turn_id is not None:
        user_turn = session.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn is not None:
            user_turn.tokens_in = None
    if emit_chat:
        session.add(
            ChatTurn(
                workspace_id=workspace_id,
                role="system",
                content={
                    "type": "turn_outcome",
                    "outcome": outcome,
                    "text": outcome_message(outcome, raw_error),
                    "turn_job_id": str(turn_job_id),
                    "user_turn_id": str(user_turn_id) if user_turn_id is not None else None,
                },
            )
        )
    kind = "cloud_turn_failed" if outcome == "failed" else "cloud_turn_cancelled"
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind=kind,
            actor_id=submitted_by_user_id,
            payload={
                "turn_job_id": str(turn_job_id),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "error": raw_error,
            },
        )
    )
    return True
