"""The public corridor-board query -- genuinely userless.

D1: "a board is the institution's published list, not any individual's
inbox." This module touches `board_items`, `calls` and `sources`, and NO
decision table and NO `user_id` at all -- unlike `boards/queue.py`, which
applies the profile OWNER's hides (2026-09-05 decisions note D2, a deliberate
divergence between the two halves of §6's "same query"). `board_queue` is not
reused here: its `_decision_predicate` call takes a required `user_id`, and
borrowing an admin's id to answer a userless request would silently apply one
person's triage to a corridor wall.

`build_dashboard_view` (web/dashboard.py) is not reusable either -- `user_id`
is a required keyword-only argument there too (`dashboard.py:377-390`).

Publication is checked by the CALLER (`web/api/public_boards.py`), not here:
an unpublished board and a nonexistent slug must produce the exact same 404
from the exact same code path (§6), which is only true if this module hands
back the SAME "board or None" for both and lets the route make the one
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.db.models import Board, BoardItem, Call, Source


@dataclass
class PublicBoardRow:
    title: str
    source_name: str
    deadline: datetime | None
    budget_min: int | None
    budget_max: int | None
    currency: str | None
    url: str


def public_board_by_slug(session: Session, *, slug: str) -> Board | None:
    """The board for `slug`, published or not.

    Deliberately does not filter on `is_published` -- the route needs to tell
    "unpublished" apart from "doesn't exist" for exactly one purpose (giving
    both the SAME 404), and folding the publication check into this query
    would make that impossible from the outside.
    """
    return session.scalar(select(Board).where(Board.slug == slug))


def public_board_items(session: Session, *, board_id: int) -> list[PublicBoardRow]:
    """The cards for one board: `board_items` JOIN `calls` JOIN `sources`,
    ordered by the admin's chosen position.

    No `CallDecision`, no `CallRanking`, no `user_id` anywhere in this
    function -- that is the whole of D1. The deadline filter mirrors
    `board_queue`'s own (boards/queue.py) only because both happen to want
    "not past", not because this calls that.
    """
    filters = [
        BoardItem.board_id == board_id,
        Call.deadline.is_not(None),
        Call.deadline > func.now(),
    ]
    rows = session.execute(
        select(Call, Source.name)
        .join(BoardItem, BoardItem.call_id == Call.id)
        .join(Source, Source.id == Call.source_id)
        .where(*filters)
        .order_by(BoardItem.position)
    ).all()
    return [
        PublicBoardRow(
            title=call.title,
            source_name=source_name,
            deadline=call.deadline,
            budget_min=call.budget_min,
            budget_max=call.budget_max,
            currency=call.currency,
            url=call.url,
        )
        for call, source_name in rows
    ]
