"""What an auto board publishes, and the job that makes it so.

The selection is a pure query: given a board, what should be on it right now.
It is separated from the writing so it can be tested without a scheduler, and
so the settings preview can call it to show an admin the four numbers before
they save.

THE ENCODE MUST NOT RUN UNDER THE BOARD ROW LOCK. `reconcile_auto_boards`
below is two passes for this reason and no other, and this is where the
reasoning lives -- its docstring used to point here for it, and this
paragraph is what it was pointing at.

`qr_for` is CPU-bound: `MAX_BOARD_ITEMS` (192, `web/api/boards.py`) items at
the measured ~45.5 ms per cold 800-byte url is ~8.7 s, and `qr.py`'s
`_encode_lock` is process-wide, so concurrent work scales that hold rather
than sharing it. The board row lock this job takes is the SAME
`SELECT ... FOR UPDATE` `update_board` takes, and `db/session.py` sets no
`lock_timeout`, so encoding under it would block a settings PUT -- and, via
`_board_for_edit` -> `_profile_for_edit`, the profile row lock 16 routes
inherit -- for that whole run. `approve_items` (`web/api/boards.py`) made
exactly this mistake once and was fixed the same way in 5e8329dd: prefetch
outside, dict-lookup inside, with a NAMED inline fallback for anything the
prefetch did not see. The hold is bounded, not gone, and both call sites say
so rather than claiming otherwise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.boards.qr import QrCode, qr_for
from grant_lab.boards.queue import board_candidate_parts
from grant_lab.db.models import Board, BoardItem, BoardSuppression, Call, CallRanking

_logger = logging.getLogger(__name__)

# `boards.last_reconcile_error` is String(500) and this text reaches an admin
# screen. A driver error can carry an entire failed statement -- parameters
# included -- so it is cut at the WRITE rather than at the render: a column that
# only ever holds what it can show cannot leak a 40 kB traceback into a page.
_MAX_RECONCILE_ERROR = 500


def _record_reconcile_outcome(
    session_factory, *, board_id: int, attempted_at: datetime, error: str | None
) -> None:
    """Stamp one board's health in its OWN short transaction.

    Not inside the board's passes, and that is the whole point: an outcome
    written in the transaction that failed is rolled back with it, leaving
    exactly the board whose health nobody can see -- the "a board silently
    stops updating" outcome this feature exists to make visible.

    Its own try/except for the same reason each board has one. A stamp that
    raises must not cost the remaining boards their night; the reconciliation
    it is describing has already committed.
    """
    try:
        with session_factory() as session:
            board = session.get(Board, board_id, with_for_update=True)
            if board is None:
                # Deleted mid-run. There is nothing left to be healthy.
                return
            board.last_reconcile_attempt_at = attempted_at
            board.last_reconcile_error = (
                None if error is None else error[:_MAX_RECONCILE_ERROR]
            )
            # ONLY on success. Leaving the old value standing on a failure is
            # what makes attempt-vs-success readable as "failing since".
            if error is None:
                board.last_reconciled_at = attempted_at
            session.commit()
    except Exception:  # noqa: BLE001 - health bookkeeping must not fail a night
        _logger.exception(
            "boards.auto_publish.health_write_failed",
            extra={"event": "boards_auto_publish_health_write_failed", "board_id": board_id},
        )


@dataclass(frozen=True)
class Selected:
    call_id: int
    url: str
    score: int


def board_selection(
    session: Session, *, board: Board, min_score: int | None = None, cap: int | None = None
) -> list[Selected]:
    """The cards an auto board should carry, best score first, already capped.

    The threshold is the FLOOR and the capacity is the CEILING: everything
    qualifying is published, across as many pages as it needs, until
    `items_per_page * max_pages`. Past that the highest-scoring win. The
    settings preview shows the truncation so the cap is never silent.

    `min_score` and `cap` are what-if overrides for that preview, which the
    spec requires to be 'live' -- the admin reads these numbers BEFORE saving.
    Both default to the stored board, so the 05:00 job's call is unchanged.

    They travel TOGETHER on purpose. A preview that overrode the threshold but
    kept the saved capacity would answer half the question and label it whole:
    lowering the floor is exactly the edit that makes `truncated` jump, and
    `truncated` is the number this preview exists to surface.
    """
    _owner_id, filters, ranking_join, order_by = board_candidate_parts(
        session, board=board, min_score=min_score
    )
    suppressed = (
        select(BoardSuppression.call_id)
        .where(BoardSuppression.board_id == board.id)
        .scalar_subquery()
    )
    if cap is None:
        cap = board.items_per_page * board.max_pages
    rows = session.execute(
        select(Call.id, Call.url, CallRanking.aggregate_score)
        .join(CallRanking, ranking_join)
        .where(*filters, Call.id.notin_(suppressed))
        # `board_queue`'s order, not a second copy of it: this `LIMIT cap` and
        # that route's page limit must keep the same candidates.
        .order_by(*order_by)
        .limit(cap)
    ).all()
    return [Selected(call_id=cid, url=url, score=score) for cid, url, score in rows]


def qualifying_count(session: Session, *, board: Board, min_score: int | None = None) -> int:
    """How many calls clear the threshold, BEFORE the capacity cap.

    The preview needs this and `len(board_selection(...))` cannot give it --
    that is already capped, which is exactly the number that hides truncation.
    """
    # `_order_by`: a COUNT has no order. Unpacked and unused so this call
    # site does not silently start ignoring a fourth part that grows a meaning.
    _owner_id, filters, ranking_join, _order_by = board_candidate_parts(
        session, board=board, min_score=min_score
    )
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
    board row lock -- this module's docstring says why, in full, and names
    the numbers.

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
        # Stamped in `finally`, so it is recorded whether the board succeeded,
        # raised, or was skipped -- and taken BEFORE the work so the timestamp
        # is when the attempt began, not when it happened to finish.
        attempted_at = datetime.now(UTC)
        failure: str | None = None
        record = True
        try:
            # PASS 1 -- no lock held. Work out what is likely wanted and encode it.
            prefetch: dict[str, QrCode] = {}
            with session_factory() as session:
                board = session.get(Board, board_id)
                if board is None:
                    # Deleted between the id query and now. No row to stamp.
                    record = False
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
                    # Either deleted, or a settings PUT turned automation off
                    # between the passes. Neither is this board's health: a
                    # manual board has no nightly run to be late.
                    record = False
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
                        # A url that CHANGED after the row was written is not
                        # a cosmetic difference: `public_board_items` serves
                        # the stored QR only while `qr_source_url ==
                        # call.url`, so from the next ingestion run the card
                        # goes out with `qr_modules=0` -- no `<svg>`, the
                        # wall's primary artefact silently gone -- and stays
                        # that way FOREVER. `BoardItem`'s own docstring names
                        # the remedy as "an admin removes the item and
                        # re-approves it", which is a manual board's answer:
                        # on an auto board nobody is in that loop, and the
                        # call keeps qualifying, so it is never removed and
                        # never re-approved. Re-encode here instead.
                        #
                        # From `prefetch` -- the SAME dict the insert branch
                        # above reads, filled in pass 1 with no lock held --
                        # so this adds no encode under the board row lock
                        # beyond the fallback that was already there for a
                        # url pass 1 never saw.
                        source = sel.url or None
                        if item.qr_source_url != source:
                            qr = (prefetch.get(sel.url) or qr_for(sel.url)) if sel.url else None
                            item.qr_path = (qr.path or None) if qr else None
                            item.qr_modules = qr.modules if qr else 0
                            item.qr_source_url = source
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
            failure = str(e)
            _logger.exception(
                "boards.auto_publish.board_failed",
                extra={
                    "event": "boards_auto_publish_board_failed",
                    "board_id": board_id,
                    "error": str(e),
                },
            )
        finally:
            # `finally`, not a line after the except: both skip paths above use
            # `continue`, which would jump straight past ordinary trailing code
            # and leave a board's health frozen at whatever it last said.
            if record:
                _record_reconcile_outcome(
                    session_factory,
                    board_id=board_id,
                    attempted_at=attempted_at,
                    error=failure,
                )
    return summary
