"""Resume-from-history serializer.

Spec (2026-04-18 design line 197): subprocess resume =
  latest chat_summaries row + all chat_turns AFTER summarized_through.

Encoded output: a list of message dicts the Claude Agent SDK can replay
as the conversation prefix when a fresh subprocess starts. The format
is `[{"role": "user"|"assistant"|"tool", "content": ...}]`. We DO NOT
wrap tool_use / tool_result content blocks here — chat_turns.content
already holds them in SDK-native shape (the design note on line 192
mandates that).

Ordering: `chat_turns.position` (a global Postgres SEQUENCE, strictly
monotonic, tie-free even within a single transaction). Do NOT order by
created_at — func.now() is statement-stable; multiple turns in one
commit can tie."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_monitor.db.models import ChatSummary, ChatTurn


def build_resume_messages(
    session: Session,
    workspace_id: uuid.UUID,
) -> list[dict]:
    """Build the SDK-compatible message list for resuming a workspace's
    conversation.

    If there is a chat_summaries row, the most recent one is rendered as
    a synthetic prefix message and only turns AFTER its summarized_through
    boundary are appended. Otherwise, all chat_turns are returned in
    position order.

    Returns [] for a workspace with no chat history at all.
    """
    # Order by the position of the summarized_through turn — that column
    # is a global Postgres SEQUENCE and is strictly monotonic even within a
    # single transaction, so it is a reliable proxy for "most recent" when
    # created_at ties (func.now() is statement-stable per transaction).
    boundary_alias = ChatTurn.__table__.alias("boundary_turn")
    summary = session.scalar(
        select(ChatSummary)
        .join(
            boundary_alias,
            ChatSummary.summarized_through == boundary_alias.c.id,
        )
        .where(ChatSummary.workspace_id == workspace_id)
        .order_by(boundary_alias.c.position.desc())
        .limit(1)
    )

    if summary is None:
        turns = session.scalars(
            select(ChatTurn)
            .where(ChatTurn.workspace_id == workspace_id)
            .order_by(ChatTurn.position)
        ).all()
        return [{"role": turn.role, "content": turn.content} for turn in turns]

    # Resolve the boundary turn's position; only turns strictly after
    # it (position greater than the boundary) survive.
    boundary = session.get(ChatTurn, summary.summarized_through)
    if boundary is None:
        # Shouldn't happen (FK ON DELETE CASCADE), but be defensive:
        # treat as no summary.
        survivors = session.scalars(
            select(ChatTurn)
            .where(ChatTurn.workspace_id == workspace_id)
            .order_by(ChatTurn.position)
        ).all()
    else:
        survivors = session.scalars(
            select(ChatTurn)
            .where(
                ChatTurn.workspace_id == workspace_id,
                ChatTurn.position > boundary.position,
            )
            .order_by(ChatTurn.position)
        ).all()

    # Synthesize the prefix message from the summary. Use a 'user' role
    # so the SDK accepts it without complaint about consecutive assistant
    # messages (the next turn will be an assistant turn after the user
    # sends their next message).
    summary_prefix = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (f"[Summary of prior conversation]\n\n{summary.summary}"),
            },
        ],
    }

    return [summary_prefix] + [{"role": turn.role, "content": turn.content} for turn in survivors]
