"""Append-only remote-escalation ledger helpers. The worker records one row
per attempt (any outcome) and counts today's rows to enforce the daily cap."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_manager.db.models import RankingRemoteAttempt


def record_attempt(
    session: Session,
    *,
    call_id: int,
    profile_id: int,
    model: str,
    outcome: str,
) -> None:
    session.add(
        RankingRemoteAttempt(
            call_id=call_id,
            profile_id=profile_id,
            model=model,
            outcome=outcome,
        )
    )


def count_today(session: Session) -> int:
    """Count attempts since the start of the current Europe/Stockholm day.
    Reuses the proposal-assist day-boundary helper so caps share one notion
    of 'today'."""
    from call_manager.timeutil import stockholm_day_start_utc

    day_start = stockholm_day_start_utc()
    return session.execute(
        select(func.count())
        .select_from(RankingRemoteAttempt)
        .where(RankingRemoteAttempt.created_at >= day_start)
    ).scalar_one()
