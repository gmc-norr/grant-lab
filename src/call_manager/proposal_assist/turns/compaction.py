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
from call_manager.proposal_assist.spend import record_spend
from call_manager.proposal_assist.turns.caps import (
    GlobalTokenCapExceeded,
    check_global_daily_token_cap,
)

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


def latest_summary_text(session: Session, workspace_id: uuid.UUID) -> str | None:
    """Return the text of the most recent compaction summary for a workspace,
    or None if there is no summary yet.

    "Most recent" is defined by joining to the boundary chat_turn and ordering
    by chat_turns.position DESC (NOT chat_summaries.created_at, which has the
    statement-time tie problem). Used by the turn processor to re-inject prior
    context into the first turn after an SDK-session reset (see
    `compact_workspace`'s reset_sdk_session and processor._prepend_summary).
    """
    return session.execute(
        select(ChatSummary.summary)
        .join(ChatTurn, ChatSummary.summarized_through == ChatTurn.id)
        .where(ChatSummary.workspace_id == workspace_id)
        .order_by(ChatTurn.position.desc())
        .limit(1)
    ).scalar_one_or_none()


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
    reset_sdk_session: bool = True,
) -> ChatSummary | None:
    """Roll un-summarized turns for one workspace into a chat_summaries
    row. Returns the new summary row, or None if there were no turns
    to summarize.

    reset_sdk_session: when True (the default — compaction is only invoked
    after needs_compaction trips), clear workspaces.last_sdk_session_id in the
    SAME transaction that writes the summary so the next turn starts a fresh
    SDK session. See the in-body comment for the full rationale.
    """
    with session_factory() as s:
        boundary_pos = _summarized_through_position(s, workspace_id)
        turns_q = select(ChatTurn).where(ChatTurn.workspace_id == workspace_id)
        if boundary_pos is not None:
            turns_q = turns_q.where(ChatTurn.position > boundary_pos)
        turns = list(s.scalars(turns_q.order_by(ChatTurn.position)))

    if not turns:
        return None

    # Compaction is a hosted-LLM call like any other. It used to spend past a
    # ceiling that had already stopped every chat turn, because it was the one
    # spender that never asked. The read session opened above is already closed
    # by here, so open our own.
    with session_factory() as cap_s:
        cap = check_global_daily_token_cap(cap_s, settings=settings)
    if not cap.allowed:
        logger.warning(
            "compaction.skipped_global_cap",
            extra={"event": "compaction_skipped_global_cap", "reason": cap.reason},
        )
        # RAISE, not return None: compact_workspace already returns None for
        # "there were no turns to summarize", and a second None would make those
        # two outcomes indistinguishable to every caller and to the test.
        raise GlobalTokenCapExceeded(
            used_in=cap.used_in,
            used_out=cap.used_out,
            cap_in=cap.cap_in,
            cap_out=cap.cap_out,
            reason=cap.reason or "cap_exceeded",
        )

    formatted_conv = "\n\n".join(
        f"[{t.role}] {json.dumps(t.content, ensure_ascii=False)}" for t in turns
    )
    user_message = _COMPACTION_PROMPT + formatted_conv

    # Bound before the try so the failure branch can tell "the call never
    # returned" (envelope is None, no usage exists) from "the call returned and
    # we failed to decode it" (envelope holds real usage that was really spent).
    # Moving the decode inside the try fixed one bug and created this one:
    # the handler recorded a zero-token row over usage sitting right there.
    envelope: dict | None = None
    try:
        envelope = summarizer_client.chat(
            messages=[{"role": "user", "content": user_message}],
        )
        # Decoding lives INSIDE the try on purpose. It used to sit below, so a
        # malformed envelope raised AFTER the hosted call had already spent
        # tokens and recorded nothing — the exact shape this ledger exists to
        # remove. Found by an outside review of the shipped branch.
        summary_text = envelope["choices"][0]["message"]["content"]
        used_model = envelope.get("model") or getattr(summarizer_client, "model", "unknown")
    except Exception:
        # Record the attempt on a FRESH session: `s` is still bound here (Python's
        # `with` does not scope its target) but it is the read session already
        # closed above, and SQLAlchemy 2.0 happily writes through a closed
        # Session — writing record_spend(s, ...) would silently pass.
        #
        # Carry whatever usage the envelope holds. If the CALL threw there is no
        # envelope and these are None -> stored 0, which is honest. If the call
        # SUCCEEDED and decoding threw, the tokens were really spent and are
        # right here; recording zero would be the same undercount this ledger
        # exists to remove.
        usage = (envelope or {}).get("usage") or {}
        with session_factory() as fail_s:
            record_spend(
                fail_s,
                workspace_id=workspace_id,
                kind="compaction",
                status="failed",
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
                model=(envelope or {}).get("model"),
            )
            fail_s.commit()
        raise

    # The ledger row is committed BEFORE the summary transaction, on its own
    # session, and deliberately NOT atomically with it. Atomicity is the wrong
    # priority here: the hosted call already happened and already cost money, so
    # a failure while persisting the summary must not roll the spend back with
    # it. The old ordering did exactly that — tokens spent, row gone, cap blind.
    # The reverse risk (spend recorded, summary lost) is the safe direction: the
    # ledger over-reports nothing, and compaction simply runs again.
    #
    # Records the REAL reported usage, not the character estimate on
    # ChatSummary.tokens (which the cap never summed). If the envelope carries no
    # usage, record None → 0: "we called it and got no counts" is honest; a
    # character estimate is not.
    with session_factory() as spend_s:
        record_spend(
            spend_s,
            workspace_id=workspace_id,
            kind="compaction",
            status="ok",
            tokens_in=(envelope.get("usage") or {}).get("prompt_tokens"),
            tokens_out=(envelope.get("usage") or {}).get("completion_tokens"),
            model=used_model,
        )
        spend_s.commit()

    with session_factory() as s:
        new_summary = ChatSummary(
            workspace_id=workspace_id,
            summary=summary_text,
            summarized_through=turns[-1].id,
            tokens=len(summary_text) // 4,  # rough token estimate
            model=used_model,
        )
        s.add(new_summary)

        # Codex M-finding: compaction was decorative — the chat_summaries row
        # was written but never fed back, so the resumed SDK session grew
        # unbounded until the daily token cap wedged the workspace. Reset the
        # SDK session so the NEXT turn starts fresh (claude_adapter only passes
        # resume=<id> when last_sdk_session_id is non-NULL — clearing it forces
        # a cold session). Done in the SAME transaction as the summary write so
        # we never clear the session without a durable summary to fall back on.
        # compact_workspace is only called once needs_compaction has tripped,
        # so a summary here always means the session was overgrown.
        #
        # Summary re-injection: the turn processor prepends this summary to the
        # FIRST turn after the reset (processor._prepend_summary, gated on
        # last_sdk_session_id IS NULL via latest_summary_text), so the fresh SDK
        # session — which has no server-side history — still gets the prior
        # context. After that turn completes, _finalize_success persists the new
        # session_id, so the injection fires exactly once per reset.
        if reset_sdk_session:
            ws = s.scalar(select(Workspace).where(Workspace.id == workspace_id))
            if ws is not None:
                ws.last_sdk_session_id = None

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
        except GlobalTokenCapExceeded:
            # The cap is org-wide, so once it trips every remaining workspace in
            # the loop would trip it too — break, don't continue. `compacted` is
            # NOT incremented, so the tick returns the number actually compacted
            # before the ceiling. This arm MUST stay above the generic
            # `except Exception`, or every capped workspace logs a stack trace.
            logger.warning(
                "compaction_tick: global daily token cap reached; stopping this tick",
                extra={"event": "compaction_tick_global_cap"},
            )
            break
        except Exception:
            logger.exception(
                "compaction_tick: compact_workspace failed for %s — continuing",
                wsid,
            )
    return compacted
