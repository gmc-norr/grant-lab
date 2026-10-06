"""Append-only remote-escalation ledger helpers. The worker records one row
per attempt (any outcome) and counts today's rows to enforce the daily cap."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.db.models import RankingRemoteAttempt


def record_attempt(
    session: Session,
    *,
    call_id: int,
    profile_id: int,
    model: str,
    outcome: str,
    error_class: str | None = None,
    usage: dict | None = None,
) -> None:
    """One row per attempt. `usage` is the envelope's OpenAI-named usage dict;
    cache reads and writes count as input (Anthropic reports them apart from
    prompt_tokens). None leaves both token columns NULL: no reply, no count."""
    tokens_in = tokens_out = None
    if usage:
        tokens_in = (
            int(usage.get("prompt_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
        )
        tokens_out = int(usage.get("completion_tokens") or 0)
    session.add(
        RankingRemoteAttempt(
            call_id=call_id,
            profile_id=profile_id,
            model=model,
            outcome=outcome,
            error_class=error_class,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
    )


def count_today(session: Session) -> int:
    """Count attempts since the start of the current Europe/Stockholm day.
    Reuses the proposal-assist day-boundary helper so caps share one notion
    of 'today'."""
    from grant_lab.timeutil import stockholm_day_start_utc

    day_start = stockholm_day_start_utc()
    return session.execute(
        select(func.count())
        .select_from(RankingRemoteAttempt)
        .where(RankingRemoteAttempt.created_at >= day_start)
    ).scalar_one()
