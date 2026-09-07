"""What an auto board publishes, and the job that makes it so.

The selection is a pure query: given a board, what should be on it right now.
It is separated from the writing so it can be tested without a scheduler, and
so the settings preview can call it to show an admin the four numbers before
they save.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.boards.qr import QrCode, qr_for
from grant_lab.boards.queue import board_candidate_parts
from grant_lab.db.models import Board, BoardItem, BoardSuppression, Call, CallRanking

_logger = logging.getLogger(__name__)


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


@dataclass
class AutoPublishSummary:
    boards_seen: int = 0
    rows_added: int = 0
    rows_removed: int = 0
    # One board that raises must not cost the others their night. Counted so
    # the tick can log a non-zero and an operator can see it.
    boards_failed: int = 0


def reconcile_auto_boards(*, session_factory) -> AutoPublishSummary:
    """Make every auto board's items match what `board_selection` says.

    Two passes per board. The encode is CPU-bound and must not run under the
    board row lock -- see this module's docstring and the shape
    `approve_items` uses since 5e8329dd.

    PER-BOARD ISOLATION. Each board's two passes sit in their own try/except.
    A bare loop would let the first board that raises -- a `qr_for` failure, an
    FK conflict, a profile deleted mid-run -- abort boards N+1..M for that
    night AND lose the summary with the exception, which is precisely the
    "a board silently stops updating" outcome this feature exists to prevent.
    APScheduler swallows the traceback into its own logger, so it would also be
    invisible.
    """
    summary = AutoPublishSummary()
    with session_factory() as session:
        board_ids = list(
            session.scalars(select(Board.id).where(Board.auto_publish.is_(True))).all()
        )

    for board_id in board_ids:
        try:
            # PASS 1 -- no lock held. Work out what is likely wanted and encode it.
            prefetch: dict[str, QrCode] = {}
            with session_factory() as session:
                board = session.get(Board, board_id)
                if board is None:
                    continue
                likely = board_selection(session, board=board)
            for sel in likely:
                if sel.url and sel.url not in prefetch:
                    prefetch[sel.url] = qr_for(sel.url)

            # PASS 2 -- under the lock, and everything is decided from the
            # LOCKED row. A settings request may have turned auto_publish off,
            # or moved min_score, between the passes; pass 1's answer is
            # advisory only.
            #
            # `with_for_update=True` is the load-bearing token in this
            # function. It is the same row lock `update_board`'s
            # `_lock_board_row` takes, and it is what stops a settings PUT
            # committing BETWEEN the re-check below and the commit at the
            # bottom -- i.e. writing items onto a board whose automation was
            # just switched off. Re-reading without the lock would pass every
            # between-the-passes test in this module and still lose that race.
            #
            # The encode is NOT free of this lock: `prefetch` is advisory, so
            # the fallback below runs `qr_for` inline, under this lock, for any
            # url pass 1 did not see (a call that started qualifying between
            # the passes). That is bounded by the same `cap` and is rare; it is
            # not zero, and this comment says so rather than claiming the hold
            # is gone -- the same wording `approve_items` uses for the same
            # fallback.
            rows_added = 0
            rows_removed = 0
            with session_factory() as session:
                board = session.get(Board, board_id, with_for_update=True)
                if board is None or not board.auto_publish:
                    continue
                wanted = {s.call_id: s for s in board_selection(session, board=board)}
                current = {
                    i.call_id: i
                    for i in session.scalars(
                        select(BoardItem).where(BoardItem.board_id == board.id)
                    ).all()
                }

                for call_id in current.keys() - wanted.keys():
                    session.delete(current[call_id])
                    rows_removed += 1

                for position, sel in enumerate(wanted.values()):
                    item = current.get(sel.call_id)
                    if item is None:
                        qr = (prefetch.get(sel.url) or qr_for(sel.url)) if sel.url else None
                        session.add(
                            BoardItem(
                                board_id=board.id,
                                call_id=sel.call_id,
                                position=position,
                                # NULL: nobody approved this. See BoardItem's
                                # own comment on the column.
                                approved_by=None,
                                qr_path=(qr.path or None) if qr else None,
                                qr_modules=qr.modules if qr else 0,
                                qr_source_url=sel.url or None,
                            )
                        )
                        rows_added += 1
                    else:
                        item.position = position
                session.commit()
            # Counted only once the commit RETURNED. Incremented inside the
            # transaction -- as this did -- anything raising after the first
            # increment leaves the summary counting rows the session's
            # rollback erased, and the tick's operator log then reports
            # `rows_added=8, boards_failed=1` for a night that wrote nothing.
            summary.boards_seen += 1
            summary.rows_added += rows_added
            summary.rows_removed += rows_removed
        except Exception as e:  # noqa: BLE001
            # Count it, name the board, keep going. Same idiom as
            # scheduler.py's `_reconcile_rankings_tick`.
            summary.boards_failed += 1
            _logger.exception(
                "boards.auto_publish.board_failed",
                extra={
                    "event": "boards_auto_publish_board_failed",
                    "board_id": board_id,
                    "error": str(e),
                },
            )
    return summary
