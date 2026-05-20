"""Bounded-replay compaction.

When a workspace accumulates many turns or many tokens, summarize the
un-summarized ones into a chat_summaries row.

SCOPE NOTE (rev2 / Codex H2): this is for the DB-stored transcript +
future UI summaries. It does NOT bound the SDK session's context.
Native session-ID resume (B3) lets the SDK manage its own conversation
history server-side; the chat_summaries table is independent of the
SDK's view. Long-running workspaces will accumulate unbounded SDK
context cost until B5/B8 adds reset-on-compaction or admin-driven
session reset.

Trigger: COMPACTION_TURN_THRESHOLD turns OR COMPACTION_TOKEN_THRESHOLD
input tokens since the latest chat_summaries boundary. Whichever trips
first.

Summarizer: the existing AnthropicClient (proposal_assist_llm_model
setting). Called with a fixed prompt; expects plain text back.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from call_manager.config import Settings
from call_manager.db.models import ChatSummary, ChatTurn, Workspace

logger = logging.getLogger(__name__)

_COMPACTION_PROMPT = """You are summarizing a portion of a proposal-drafting conversation.

Summarize the conversation below into a compact briefing that captures:
- The user's overall intent and any specific asks they've made
- The agent's working approach (tool calls, files touched, decisions)
- The current state of the proposal artifact (sections completed, pending)
- Any unresolved questions or pending decisions

Keep the summary under 2000 tokens. Do not include direct quotes unless essential.
Write as a coherent briefing, not bullet points.

Conversation:
"""


def _summarized_through_position(session: Session, workspace_id: uuid.UUID) -> int | None:
    """Return the `position` of the chat_turn the latest summary covers,
    or None if there is no summary yet.

    "Latest" is defined by joining to the boundary chat_turn and ordering
    by chat_turns.position DESC — NOT chat_summaries.created_at. The
    created_at column has the same statement-time tie problem that B3
    fixed for chat_turns ordering: multiple summaries written in one tx
    share an identical timestamp.
    """
    boundary_pos = session.execute(
        select(ChatTurn.position)
        .join(ChatSummary, ChatSummary.summarized_through == ChatTurn.id)
        .where(ChatSummary.workspace_id == workspace_id)
        .order_by(ChatTurn.position.desc())
        .limit(1)
    ).scalar_one_or_none()
    return int(boundary_pos) if boundary_pos is not None else None


def _un_summarized_stats(
    session: Session,
    workspace_id: uuid.UUID,
) -> tuple[int, int]:
    """Return (count, sum_tokens_in) of chat_turns NOT yet summarized."""
    boundary = _summarized_through_position(session, workspace_id)
    base_q = select(
        func.count(ChatTurn.id),
        func.coalesce(func.sum(ChatTurn.tokens_in), 0),
    ).where(ChatTurn.workspace_id == workspace_id)
    if boundary is not None:
        base_q = base_q.where(ChatTurn.position > boundary)
    count, tokens = session.execute(base_q).one()
    return int(count), int(tokens)


def needs_compaction(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    settings: Settings,
) -> bool:
    count, tokens = _un_summarized_stats(session, workspace_id)
    return (
        count >= settings.compaction_turn_threshold or tokens >= settings.compaction_token_threshold
    )


def compact_workspace(
    *,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    summarizer_client: Any,  # duck-typed: AnthropicClient or stub
    settings: Settings,
) -> ChatSummary | None:
    """Roll un-summarized turns for one workspace into a chat_summaries
    row. Returns the new summary row, or None if there were no turns
    to summarize."""
    with session_factory() as s:
        boundary_pos = _summarized_through_position(s, workspace_id)
        turns_q = select(ChatTurn).where(ChatTurn.workspace_id == workspace_id)
        if boundary_pos is not None:
            turns_q = turns_q.where(ChatTurn.position > boundary_pos)
        turns = list(s.scalars(turns_q.order_by(ChatTurn.position)))

    if not turns:
        return None

    formatted_conv = "\n\n".join(
        f"[{t.role}] {json.dumps(t.content, ensure_ascii=False)}" for t in turns
    )
    user_message = _COMPACTION_PROMPT + formatted_conv

    envelope = summarizer_client.chat(
        messages=[{"role": "user", "content": user_message}],
    )
    summary_text = envelope["choices"][0]["message"]["content"]
    used_model = envelope.get("model") or getattr(summarizer_client, "model", "unknown")

    with session_factory() as s:
        new_summary = ChatSummary(
            workspace_id=workspace_id,
            summary=summary_text,
            summarized_through=turns[-1].id,
            tokens=len(summary_text) // 4,  # rough token estimate
            model=used_model,
        )
        s.add(new_summary)
        s.commit()
        s.refresh(new_summary)
        return new_summary


def run_compaction_tick(
    *,
    session_factory: sessionmaker,
    summarizer_client_factory: Callable[[], Any],
    settings: Settings,
) -> int:
    """Scan all workspaces; for any over threshold, call compact_workspace.
    Returns the count of workspaces compacted.

    The factory pattern lets the scheduler construct the AnthropicClient
    lazily — same shape as the Phase 0 tick — so a missing API key
    surfaces only when work is actually needed."""
    with session_factory() as s:
        workspace_ids = list(
            s.scalars(select(Workspace.id).where(Workspace.soft_deleted_at.is_(None)))
        )

    compacted = 0
    client: Any | None = None
    for wsid in workspace_ids:
        with session_factory() as s:
            if not needs_compaction(s, wsid, settings=settings):
                continue
        if client is None:
            client = summarizer_client_factory()
        try:
            compact_workspace(
                session_factory=session_factory,
                workspace_id=wsid,
                summarizer_client=client,
                settings=settings,
            )
            compacted += 1
        except Exception:
            logger.exception(
                "compaction_tick: compact_workspace failed for %s — continuing",
                wsid,
            )
    return compacted
