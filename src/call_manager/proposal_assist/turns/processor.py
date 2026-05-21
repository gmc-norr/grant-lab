"""Process one queued turn end-to-end.

Called by the drain scheduler tick (one turn per workspace per tick).
Also invoked directly by the integration tests for deterministic
verification.

H1 ordering: controller.launch() runs BEFORE the atomic claim. The
launch triggers B3's orphan sweep, which marks any stale `running`
turn_jobs row as `failed/orphaned`. If we claimed first, the sweep
would kill our just-claimed row. If we didn't sweep first, a stale
running row would block our claim via the unique partial index on
turn_jobs(workspace_id) WHERE status='running'.

The cached-handle path also satisfies H1: a cached handle proves no
crash since launch, so no stale rows can exist.

Responsibilities:
  - Peek workspace_id + status (read-only). Return early if the row
    isn't `queued` — avoids firing launch (whose orphan sweep would
    clobber an already-running row).
  - Launch (or reuse) the workspace's subprocess. If launch itself
    raises (e.g., LockNotAcquired in a multi-instance future, or SDK
    init failure), log and return — the row stays `queued` and the
    next drain tick retries. NB this leaves the submit-time
    `cloud_turn_sent` audit row without a matching outcome event;
    persistent launch failure → N un-matched cloud_turn_sent rows for
    one turn_job_id. B6/B8 may want to write `cloud_turn_failed`
    after K retries — out of scope for B4.
  - Atomically claim the queued turn (queued → running).
  - Read the user message from the linked chat_turns row. If the row
    is missing (impossible under normal submit flow), mark failed via
    _finalize_failed (which DOES write cloud_turn_failed).
  - Dispatch via WorkspaceController.send_turn (async generator).
  - For each StreamEvent, write a chat_turns row. Each row commits
    independently — a mid-stream crash leaves partial assistant/tool
    rows in place; the orphan sweep marks the turn_job failed but
    does NOT cascade-delete chat_turns. B6 UI will need to render
    partial-then-failed transcripts coherently.
  - On TurnDoneEvent: stamp tokens on the latest assistant row,
    persist session_id on the workspace, mark turn_job completed,
    write the cloud_turn_completed audit event.
  - On SDKTurnError: mark turn_job failed, write the
    cloud_turn_failed audit event.
  - On any other exception: same as SDKTurnError but with a
    processor_failure prefix on the error message; re-raise
    non-Exception BaseExceptions (CancelledError).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    SDKTurnError,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.controller import WorkspaceController

logger = logging.getLogger(__name__)


async def process_turn_once(
    *,
    session_factory: sessionmaker,
    controller: WorkspaceController,
    turn_job_id: uuid.UUID,
) -> None:
    """Process one queued turn. No-op if the row is not in 'queued' state."""
    # Peek to learn the workspace_id and current status without claiming yet.
    with session_factory() as s:
        row = s.execute(
            select(TurnJob.workspace_id, TurnJob.status).where(TurnJob.id == turn_job_id)
        ).first()
    if row is None:
        logger.info("turn_job %s does not exist; skipping", turn_job_id)
        return
    workspace_id_for_launch, peek_status = row
    if peek_status != "queued":
        logger.info(
            "turn_job %s is in status %r (not queued); skipping",
            turn_job_id,
            peek_status,
        )
        return

    # Launch BEFORE claim — runs orphan sweep on cold subprocess. Stale
    # `running` rows get marked `failed/orphaned`, so our subsequent
    # claim doesn't trip the unique-running index.
    try:
        await controller.launch(workspace_id_for_launch)
    except Exception:
        logger.exception(
            "process_turn_once: controller.launch failed for workspace %s; "
            "leaving turn_job %s in queued state for retry",
            workspace_id_for_launch,
            turn_job_id,
        )
        return

    # Atomic claim AFTER the sweep has run. RETURNING also brings back
    # submitted_by_user_id so the outcome events (cloud_turn_completed /
    # cloud_turn_failed) carry actor_id — matches the submit-time
    # cloud_turn_sent event and gives compliance a single-row "who"
    # answer without a turn_jobs join.
    with session_factory() as s:
        result = s.execute(
            update(TurnJob)
            .where(TurnJob.id == turn_job_id, TurnJob.status == "queued")
            .values(status="running", started_at=datetime.now(UTC))
            .returning(
                TurnJob.workspace_id,
                TurnJob.user_turn_id,
                TurnJob.submitted_by_user_id,
            )
        ).first()
        s.commit()
        if result is None:
            logger.info("turn_job %s already claimed or completed; skipping", turn_job_id)
            return
        workspace_id, user_turn_id, submitted_by_user_id = result

    with session_factory() as s:
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        if user_turn is None:
            logger.error("turn_job %s has no user_turn_id row", turn_job_id)
            _finalize_failed(
                session_factory,
                turn_job_id=turn_job_id,
                workspace_id=workspace_id,
                submitted_by_user_id=submitted_by_user_id,
                error="missing_user_turn_row",
                tokens_in=0,
                tokens_out=0,
            )
            return
        user_text = _content_to_text(user_turn.content)

    last_assistant_id: uuid.UUID | None = None
    tokens_in = 0
    tokens_out = 0
    model: str | None = None
    session_id: str | None = None
    sdk_error: SDKTurnError | None = None
    other_error: BaseException | None = None

    try:
        async for event in controller.send_turn(workspace_id, user_text):
            if isinstance(event, AssistantTextEvent):
                last_assistant_id = _write_chat_turn(
                    session_factory,
                    workspace_id,
                    role="assistant",
                    content={"type": "text", "text": event.text},
                )
            elif isinstance(event, ToolUseEvent):
                last_assistant_id = _write_chat_turn(
                    session_factory,
                    workspace_id,
                    role="assistant",
                    content={
                        "type": "tool_use",
                        "id": event.tool_use_id,
                        "name": event.tool_name,
                        "input": event.input,
                    },
                )
            elif isinstance(event, ToolResultEvent):
                _write_chat_turn(
                    session_factory,
                    workspace_id,
                    role="tool",
                    content={
                        "type": "tool_result",
                        "tool_use_id": event.tool_use_id,
                        "content": event.content,
                        "is_error": event.is_error,
                    },
                )
            elif isinstance(event, TurnDoneEvent):
                tokens_in = event.tokens_in or 0
                tokens_out = event.tokens_out or 0
                model = event.model
                session_id = event.session_id
    except SDKTurnError as e:
        sdk_error = e
    except BaseException as e:  # noqa: BLE001
        other_error = e

    if sdk_error is None and other_error is None:
        _finalize_success(
            session_factory,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            submitted_by_user_id=submitted_by_user_id,
            last_assistant_id=last_assistant_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            session_id=session_id,
        )
    elif sdk_error is not None:
        status_suffix = f"; status={sdk_error.status}" if sdk_error.status is not None else ""
        _finalize_failed(
            session_factory,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            submitted_by_user_id=submitted_by_user_id,
            error=f"sdk_turn_error: {sdk_error}{status_suffix}",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    else:
        assert other_error is not None
        _finalize_failed(
            session_factory,
            turn_job_id=turn_job_id,
            workspace_id=workspace_id,
            submitted_by_user_id=submitted_by_user_id,
            error=f"processor_failure: {type(other_error).__name__}: {other_error}",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        if not isinstance(other_error, Exception):
            raise other_error


def _content_to_text(content: Any) -> str:
    """Extract a string for the SDK. Handles plain string, single dict,
    or list-of-dicts (mirrors claude_adapter._flatten_content)."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return ""
    if isinstance(content, list):
        return " ".join(_content_to_text(b) for b in content if _content_to_text(b))
    return ""


def _write_chat_turn(
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    *,
    role: str,
    content: dict[str, Any],
) -> uuid.UUID:
    with session_factory() as s:
        turn = ChatTurn(workspace_id=workspace_id, role=role, content=content)
        s.add(turn)
        s.commit()
        return turn.id


def _finalize_success(
    session_factory: sessionmaker,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    submitted_by_user_id: int | None,
    last_assistant_id: uuid.UUID | None,
    tokens_in: int,
    tokens_out: int,
    model: str | None,
    session_id: str | None,
) -> None:
    with session_factory() as s:
        if last_assistant_id is not None:
            turn = s.scalar(select(ChatTurn).where(ChatTurn.id == last_assistant_id))
            if turn is not None:
                turn.tokens_in = tokens_in or None
                turn.tokens_out = tokens_out or None
                turn.model = model
        job = s.scalar(select(TurnJob).where(TurnJob.id == turn_job_id))
        if job is not None:
            job.status = "completed"
            job.completed_at = datetime.now(UTC)
        if session_id is not None:
            ws = s.scalar(select(Workspace).where(Workspace.id == workspace_id))
            if ws is not None:
                ws.last_sdk_session_id = session_id
        s.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="cloud_turn_completed",
                actor_id=submitted_by_user_id,
                payload={
                    "turn_job_id": str(turn_job_id),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": model,
                },
            )
        )
        s.commit()


def _finalize_failed(
    session_factory: sessionmaker,
    *,
    turn_job_id: uuid.UUID,
    workspace_id: uuid.UUID,
    submitted_by_user_id: int | None,
    error: str,
    tokens_in: int,
    tokens_out: int,
) -> None:
    with session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == turn_job_id))
        if job is not None:
            job.status = "failed"
            job.error = error
            job.completed_at = datetime.now(UTC)
        s.add(
            WorkspaceEvent(
                workspace_id=workspace_id,
                kind="cloud_turn_failed",
                actor_id=submitted_by_user_id,
                payload={
                    "turn_job_id": str(turn_job_id),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "error": error,
                },
            )
        )
        s.commit()
