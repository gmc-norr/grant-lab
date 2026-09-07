"""The admin approval queue for one board.

WHOSE HIDES. Plan B closed with F-10: `_decision_predicate` requires a user_id
and §4.3's board is profile-scoped with no user in scope. The two halves get
different answers. The PUBLIC board (boards/public.py) applies NO decisions --
spec §6, and correct semantics anyway, since a board is the institution's
published list and not any individual's inbox. This QUEUE applies the PROFILE
OWNER's, which is the 2026-09-05 decisions note D2 and a deliberate override of
§6's "the same query ... feeds the admin approval queue".

Key off `profiles.created_by`, NOT `boards.created_by`. §4.3 gives board creation
to any profile EDITOR, and "can edit" is `created_by == user.id or user.is_admin`
(web/api/profiles.py:261), so an admin can build a board on a profile they do not
own. Keying off the board's creator would then apply a passing admin's inbox
housekeeping to somebody else's corridor wall.

This CALLS dashboard._decision_predicate. It does not copy it. A second copy is
how the rail and the feed drift apart under a filter nobody re-tested -- see that
function's own docstring.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.db.models import Board, BoardItem, Call, CallRanking, Profile
from grant_lab.web.dashboard import _decision_predicate


@dataclass
class QueueRow:
    call: Call
    score: int
    already_approved: bool


def board_candidate_parts(session: Session, *, board: Board):
    """The filters, join AND ORDER every view of a board's candidates shares.

    `board_queue` (the admin's list) and `boards/auto_publish.board_selection`
    (what actually goes up) MUST agree about what qualifies. A second copy is
    how they drift under a filter nobody re-tested -- this module's own
    docstring makes the same argument about `_decision_predicate`.

    The ORDER is part of that contract, not decoration. Both callers apply a
    `LIMIT` -- `board_queue`'s page size, `board_selection`'s
    `items_per_page * max_pages` capacity -- so the ordering decides WHICH
    candidates survive it. Two independent copies of
    `aggregate_score.desc(), Call.id.desc()` agree only until one of them
    moves, and then the admin's top-24 and the corridor wall's 24 are
    different calls with nothing red to say so. Returned here so there is
    one copy.
    """
    owner_id = session.scalar(select(Profile.created_by).where(Profile.id == board.profile_id))
    filters = [
        _decision_predicate("archive", user_id=owner_id, profile_id=board.profile_id),
        CallRanking.aggregate_score >= board.min_score,
        Call.deadline.is_not(None),
        Call.deadline > func.now(),
    ]
    # Scope the ranking join to the BOARD'S OWN profile. A call ranked on
    # several profiles has a CallRanking row per profile; without this clause
    # the join fans out one row per profile and judges `min_score` against a
    # score another group produced.
    ranking_join = (CallRanking.call_id == Call.id) & (CallRanking.profile_id == board.profile_id)
    # Best score first; `Call.id.desc()` only breaks ties, so the order is
    # total and two runs over an unchanged table return the same page.
    order_by = (CallRanking.aggregate_score.desc(), Call.id.desc())
    return owner_id, filters, ranking_join, order_by


def board_queue(
    session: Session, *, board: Board, limit: int, offset: int
) -> tuple[list[QueueRow], int]:
    """Candidates for `board`, best score first, plus the WAITING count.

    The int is not a pagination total: already-approved candidates are
    returned in the rows (flagged, not dropped -- D3) but excluded from this
    count, so it is smaller than the returned population and must not be used
    to paginate them.
    """
    # `_owner_id`: MEASURED dead after the extraction -- its only use in this
    # function was the `_decision_predicate` call that just moved. Underscored
    # so the next reader is not hunting for a use that is not there.
    _owner_id, filters, ranking_join, order_by = board_candidate_parts(session, board=board)
    approved_ids = select(BoardItem.call_id).where(BoardItem.board_id == board.id).scalar_subquery()

    rows = session.execute(
        select(Call, CallRanking.aggregate_score, Call.id.in_(approved_ids))
        .join(CallRanking, ranking_join)
        .where(*filters)
        .order_by(*order_by)
        .limit(limit)
        .offset(offset)
    ).all()
    queue_rows = [
        QueueRow(call=call, score=score, already_approved=bool(approved))
        for call, score, approved in rows
    ]

    # Already-approved candidates stay in `queue_rows` (flagged, not dropped --
    # D3: approval and the queue are different states) but drop out of the
    # WAITING count, which is what a "N calls need review" badge means.
    waiting = session.scalar(
        select(func.count())
        .select_from(Call)
        .join(CallRanking, ranking_join)
        .where(*filters, Call.id.notin_(approved_ids))
    )

    return queue_rows, waiting
