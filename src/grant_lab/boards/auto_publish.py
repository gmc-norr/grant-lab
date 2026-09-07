"""What an auto board publishes, and the job that makes it so.

The selection is a pure query: given a board, what should be on it right now.
It is separated from the writing so it can be tested without a scheduler, and
so the settings preview can call it to show an admin the four numbers before
they save.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.boards.queue import board_candidate_parts
from grant_lab.db.models import Board, BoardSuppression, Call, CallRanking


@dataclass(frozen=True)
class Selected:
    call_id: int
    url: str
    score: int


def board_selection(session: Session, *, board: Board) -> list[Selected]:
    """The cards an auto board should carry, best score first, already capped.

    The threshold is the FLOOR and the capacity is the CEILING: everything
    qualifying is published, across as many pages as it needs, until
    `items_per_page * max_pages`. Past that the highest-scoring win. The
    settings preview shows the truncation so the cap is never silent.
    """
    _owner_id, filters, ranking_join = board_candidate_parts(session, board=board)
    suppressed = (
        select(BoardSuppression.call_id)
        .where(BoardSuppression.board_id == board.id)
        .scalar_subquery()
    )
    cap = board.items_per_page * board.max_pages
    rows = session.execute(
        select(Call.id, Call.url, CallRanking.aggregate_score)
        .join(CallRanking, ranking_join)
        .where(*filters, Call.id.notin_(suppressed))
        .order_by(CallRanking.aggregate_score.desc(), Call.id.desc())
        .limit(cap)
    ).all()
    return [Selected(call_id=cid, url=url, score=score) for cid, url, score in rows]


def qualifying_count(session: Session, *, board: Board) -> int:
    """How many calls clear the threshold, BEFORE the capacity cap.

    The preview needs this and `len(board_selection(...))` cannot give it --
    that is already capped, which is exactly the number that hides truncation.
    """
    _owner_id, filters, ranking_join = board_candidate_parts(session, board=board)
    suppressed = (
        select(BoardSuppression.call_id)
        .where(BoardSuppression.board_id == board.id)
        .scalar_subquery()
    )
    return session.scalar(
        select(func.count())
        .select_from(Call)
        .join(CallRanking, ranking_join)
        .where(*filters, Call.id.notin_(suppressed))
    )
