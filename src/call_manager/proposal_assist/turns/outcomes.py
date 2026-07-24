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
    usage_unknown: bool = False,
) -> bool:
    """Record a terminal failed/cancelled outcome in the caller's transaction.

    Returns True if this call performed the transition, False if the job was
    already terminal / in the wrong workspace (no double-finalize). The caller
    commits.

    usage_unknown: True when the turn is terminalized while RUNNING and its real
        usage is not yet known (workspace deletion). Retains the submit-time
        estimate so the spend stays in the daily-cap sum; stamp_post_terminal_usage
        later replaces it with the SDK-reported truth.
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
    consumed = tokens_in > 0 or tokens_out > 0
    # B9 cap-accounting fix: a turn that streamed tokens before failing actually
    # spent them, so they MUST count against the daily cap (which sums
    # ChatTurn.tokens_{in,out} for today). Previously the consumed tokens lived
    # ONLY in the WorkspaceEvent audit payload below — which the cap query never
    # reads — so a failed turn recorded ZERO against the cap, letting a looping
    # user burn real spend invisibly. Stamp the consumed tokens onto a chat_turns
    # row so the cap sees them. When NOTHING was consumed (the common
    # reaped/orphaned/cancelled case) clear the submit-time pre-count so the
    # abandoned turn doesn't keep burning budget for input it never used —
    # UNLESS usage_unknown, where the turn really was running and its input was
    # really spent, we just cannot measure it yet.
    if user_turn_id is not None:
        user_turn = session.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn is not None and not consumed and not usage_unknown:
            user_turn.tokens_in = None
    if emit_chat:
        # Stamp the actually-consumed tokens onto the viewer-safe system outcome
        # row (the user-turn pre-count is an ESTIMATE the SDK totals supersede, so
        # we always clear it when consumed and record the truth here instead). The
        # system row carries None/None when nothing was consumed.
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
                tokens_in=tokens_in or None,
                tokens_out=tokens_out or None,
            )
        )
        # The pre-count is now superseded by the system row's truth; clear it so
        # the cap sum doesn't double-count this turn's input.
        if consumed and user_turn_id is not None:
            user_turn = session.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
            if user_turn is not None:
                user_turn.tokens_in = None
    elif consumed and user_turn_id is not None:
        # emit_chat=False (soft-delete: no surface row) but tokens WERE consumed.
        # There is no system row to carry them, so replace the user-turn pre-count
        # estimate with the SDK-reported truth on the user-turn row itself rather
        # than dropping the spend from the cap sum.
        user_turn = session.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn is not None:
            user_turn.tokens_in = tokens_in or None
            user_turn.tokens_out = tokens_out or None
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


USAGE_RECONCILED_KIND = "cloud_turn_usage_reconciled"

_TERMINAL_STATUSES = ("failed", "cancelled", "completed")


def stamp_post_terminal_usage(
    session: Session,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_turn_id: uuid.UUID | None,
    last_assistant_id: uuid.UUID | None,
    tokens_in: int,
    tokens_out: int,
    model: str | None,
) -> bool:
    """Record a terminalized turn's REAL usage after the fact.

    Used when a turn was terminalized while RUNNING (workspace deletion) and its
    stream later reported actual usage, so the normal finalize lost the row and
    rolled its token stamp back. Writes the SDK truth and CLEARS the retained
    submit-time estimate (mirroring _finalize_success) so the daily cap counts the
    spend exactly once. NEVER changes TurnJob.status; emits ONE non-outcome
    ``cloud_turn_usage_reconciled`` audit event rather than a second outcome.

    Concurrency: serialized on the TurnJob row (SELECT ... FOR UPDATE) — the event
    lookup alone is not atomic (JSONB gives no uniqueness), so two callers could
    otherwise both stamp. Returns True only for the caller that performed the
    stamp. Caller commits.
    """
    job = session.execute(
        select(TurnJob)
        .where(TurnJob.id == turn_job_id, TurnJob.workspace_id == workspace_id)
        .with_for_update()
    ).scalar_one_or_none()
    if job is None or job.status not in _TERMINAL_STATUSES:
        return False

    already = session.scalar(
        select(WorkspaceEvent.id).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.kind == USAGE_RECONCILED_KIND,
            WorkspaceEvent.payload["turn_job_id"].astext == str(turn_job_id),
        )
    )
    if already is not None:
        return False

    user_turn = (
        session.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn_id is not None
        else None
    )
    if last_assistant_id is not None:
        assistant = session.scalar(select(ChatTurn).where(ChatTurn.id == last_assistant_id))
        if assistant is not None:
            assistant.tokens_in = tokens_in or None
            assistant.tokens_out = tokens_out or None
            assistant.model = model
        # The SDK total already includes user-input tokens; drop the estimate so
        # the cap sum doesn't count input twice (mirrors _finalize_success).
        if user_turn is not None:
            user_turn.tokens_in = None
    elif user_turn is not None:
        # No assistant row — persist totals on the user row, REPLACING the estimate.
        user_turn.tokens_in = tokens_in or None
        user_turn.tokens_out = tokens_out or None
        if model is not None:
            user_turn.model = model

    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind=USAGE_RECONCILED_KIND,
            actor_id=None,
            payload={
                "turn_job_id": str(turn_job_id),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": model,
            },
        )
    )
    return True
