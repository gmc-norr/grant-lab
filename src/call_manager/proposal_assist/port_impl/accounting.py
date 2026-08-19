"""SqlAccounting — one conditional statement per operation, owner-bound.

No shared lease and no generation predicate, by design (spec 3.3). What replaces
the fence is OWNERSHIP: both statements target the turn job that originated the
work, so an actor from a previous generation cannot reserve or refund the slot of
a turn that started after the workspace moved on.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.ports.accounting import MAX_FETCHES_PER_TURN


class SqlAccounting:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sessions = session_factory

    def reserve_fetch_slot(self, active_turn_job_id: uuid.UUID) -> int | None:
        with self._sessions.begin() as session:
            row = session.execute(
                update(TurnJob)
                .where(
                    TurnJob.id == active_turn_job_id,
                    TurnJob.status == "running",
                    TurnJob.fetch_count < MAX_FETCHES_PER_TURN,
                )
                .values(fetch_count=TurnJob.fetch_count + 1)
                .returning(TurnJob.fetch_count)
            ).first()
            return None if row is None else row[0]

    def refund_fetch_slot(self, active_turn_job_id: uuid.UUID) -> None:
        with self._sessions.begin() as session:
            session.execute(
                update(TurnJob)
                .where(
                    TurnJob.id == active_turn_job_id,
                    TurnJob.status == "running",
                    TurnJob.fetch_count > 0,
                )
                .values(fetch_count=TurnJob.fetch_count - 1)
            )
