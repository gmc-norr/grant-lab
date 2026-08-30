"""The one place hosted-LLM spend is written, and the one place it is read.

Before this, spend was RE-DERIVED at read time by summing five tables on three
different timestamp columns. Every new feature had to remember two columns, a
write on every terminal path, and a SELECT in caps.py — and six sites had got it
wrong, every one of them in the same direction: money spent, nothing recorded.

Rules for callers:
  * Record on EVERY terminal path, including failures and timeouts. A run that
    errored after consuming input tokens cost real money.
  * Record even when the token counts are None. "We spent nothing" and "we never
    looked" are different facts; a missing row cannot express the second.
  * The caller commits. This only adds to the session, so the row normally lands
    in the same transaction as the job's own status change and cannot half-happen.
  * ONE deliberate exception: turns/compaction.py commits its success row on its
    own session BEFORE writing the summary. Atomicity is the wrong priority once
    the money is already spent — a failure persisting the summary must not roll
    the row back with it. Prefer the same transaction; break it only where the
    spend would otherwise be lost, and say so at the call site.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_manager.db.models import LlmSpend
from call_manager.timeutil import stockholm_day_start_utc

SpendKind = Literal[
    "chat_turn",
    "compaction",
    "coherence",
    "evaluator",
    "review",
    "uncited_claims",
    "citation",
]
SpendStatus = Literal["ok", "failed", "timeout"]

__all__ = ["SpendKind", "SpendStatus", "record_spend", "sum_spend_today"]


def record_spend(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    kind: SpendKind,
    status: SpendStatus,
    tokens_in: int | None = 0,
    tokens_out: int | None = 0,
    model: str | None = None,
    ref_id: str | uuid.UUID | int | None = None,
    sdk_session_id: str | None = None,
) -> None:
    """Append one spend row. The CALLER COMMITS.

    `tokens_in`/`tokens_out` accept None and store 0 — the point is that the row
    exists. `ref_id` accepts whatever the owning table keys on (uuid for jobs,
    int for chat turns, None for compaction, which has no row) and is stored as
    text, so one column serves all of them.
    """
    session.add(
        LlmSpend(
            workspace_id=workspace_id,
            kind=kind,
            ref_id=None if ref_id is None else str(ref_id),
            status=status,
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            model=model,
            sdk_session_id=sdk_session_id,
        )
    )


def sum_spend_today(
    session: Session,
    *,
    workspace_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """`(tokens_in, tokens_out)` since Stockholm midnight, all kinds.

    `now` is a parameter for the same reason `stockholm_day_start_utc` takes one:
    a function that reads the clock itself cannot be falsified by a test.
    """
    stmt = select(
        func.coalesce(func.sum(LlmSpend.tokens_in), 0),
        func.coalesce(func.sum(LlmSpend.tokens_out), 0),
    ).where(LlmSpend.created_at >= stockholm_day_start_utc(now))
    if workspace_id is not None:
        stmt = stmt.where(LlmSpend.workspace_id == workspace_id)
    row = session.execute(stmt).one()
    return int(row[0]), int(row[1])
