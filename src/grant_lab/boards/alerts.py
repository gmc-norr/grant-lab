"""D9: tell a board's profile owner when it has gone underfull.

MATCH-edition module. Nothing here may import from grant_lab.proposal_assist --
see `web/api/boards.py`'s own module docstring for why that gate bites.

THE TRIGGER (Open-2): a PUBLISHED board whose count of RENDERABLE approved
items -- `board_items` joined to `calls`, `Call.deadline` in the future -- is
below the board's STORED `items_per_page` (D7: an author-set override, never
re-derived from `ORIENTATION_ITEMS_PER_PAGE`). `boards_needing_attention`
computes this from `boards` and `_renderable_counts` (`board_items` joined to
`calls`). Nothing else -- `_renderable_counts` mirrors
`boards/public.py:public_board_items`'s own "not past" filter on purpose: a
board's alert threshold has to agree with what the public screen would
actually render, not with what an admin approved that has since expired.

THE RECIPIENT (D9): `profiles.created_by` for the board's OWN
`profile_id` -- the same person `boards/queue.py` keys its own hides off of
-- and NOT `boards.created_by`, which may be an admin who built the board for
somebody else (spec Sec 4.3 lets any profile editor create one). Telling that
admin a queue they do not own is short-telling the wrong person.

THE CHANNEL. `profiles/create.py:79` inserts a `Subscription` row for every
profile owner inside the same atomic create as the profile itself, so
`subscriptions` is a per-(user, profile) row for every profile owner today --
but it is a CONVENTION, not a constraint: no CHECK, FK or unique index
enforces it, and `scripts/seed_dev_data.py:328` writes its row in a separate
transaction after its own commit. `_alert_for` therefore ASSERTS the row
exists for the resolved owner rather than trusting an iteration over
whatever subscription rows happen to exist, and reports a missing row the
same way it reports an undeliverable owner -- escalated to admins, flagged
`owner_undeliverable` -- rather than silently skipping the board. A
`for sub in subscriptions:` loop would skip a missing row and pass every
other case; see `tests/test_boards_alerts.py`'s
`test_a_MISSING_subscription_row_is_reported_not_skipped`.

THE MUTE SWITCH is `Subscription.board_alerts_enabled` (Open-1), never
`digest_enabled` -- muting the weekly email summary must not also mute the
alert that a public corridor screen has gone short.

THE ESCALATION (Open-3): when the owner is undeliverable --
`User.status != 'active'` OR `digest_disabled_at IS NOT NULL`, the same two
conditions `digest/send.py:_deliverable_clause` already filters the weekly
digest on -- the recipients become every active admin instead, and
`owner_undeliverable` is True so `web/api/boards.py` can surface the
suppression on the board detail screen (half 2 of Open-3: "a delivery the
system knows it suppressed is the one case where an in-app surface is not
optional").
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.db.models import Board, BoardItem, Call, Profile, Subscription, User
from grant_lab.digest.send import _deliverable_clause


@dataclass(frozen=True)
class BoardAlert:
    board_id: int
    shortfall: int  # items_per_page minus renderable items
    recipient_ids: list[int]  # the owner, or the escalation set
    owner_undeliverable: bool  # Open-3 half 2: shown in-app


def _active_admin_ids(session: Session) -> list[int]:
    """Every active admin, id-ordered for a deterministic recipient list --
    the same `is_admin` + `status == 'active'` predicate
    `web/api/admin_users.py`'s own active-admin-count lock uses."""
    return list(
        session.execute(
            select(User.id)
            .where(User.is_admin.is_(True), User.status == "active")
            .order_by(User.id)
        ).scalars()
    )


def _renderable_counts(session: Session, board_ids: list[int]) -> dict[int, int]:
    """board_id -> count of its RENDERABLE approved items. Reads
    `board_items` joined to `calls` only -- one query for however many boards
    are being checked, not one per board (same shape as `_approved_counts` in
    `web/api/boards.py`)."""
    if not board_ids:
        return {}
    rows = session.execute(
        select(BoardItem.board_id, func.count())
        .join(Call, Call.id == BoardItem.call_id)
        .where(
            BoardItem.board_id.in_(board_ids),
            Call.deadline.is_not(None),
            Call.deadline > func.now(),
        )
        .group_by(BoardItem.board_id)
    ).all()
    return dict(rows)


def _alert_for(
    session: Session, *, board_id: int, profile_id: int, shortfall: int, admin_ids: list[int]
) -> BoardAlert | None:
    """The alert for one already-confirmed-underfull board, or None if its
    owner has muted board alerts. `owner_id` is resolved from
    `profiles.created_by` -- never `boards.created_by` -- before anything
    else, so the Subscription lookup below is a targeted assertion on THAT
    person's row, not an iteration a missing row could silently fall through.

    `admin_ids` is computed once by the caller and threaded through rather
    than requeried per escalating board.
    """
    owner_id = session.scalar(select(Profile.created_by).where(Profile.id == profile_id))
    sub = session.execute(
        select(Subscription).where(
            Subscription.user_id == owner_id, Subscription.profile_id == profile_id
        )
    ).scalar_one_or_none()
    if sub is None:
        # D10: the convention is violated. Report it the same way an
        # undeliverable owner is reported -- do not guess a default and do
        # not skip.
        return BoardAlert(
            board_id=board_id,
            shortfall=shortfall,
            recipient_ids=admin_ids,
            owner_undeliverable=True,
        )
    if not sub.board_alerts_enabled:
        return None

    owner_reachable = (
        session.scalar(select(User.id).where(User.id == owner_id, _deliverable_clause()))
        is not None
    )
    if owner_reachable:
        return BoardAlert(
            board_id=board_id,
            shortfall=shortfall,
            recipient_ids=[owner_id],
            owner_undeliverable=False,
        )
    return BoardAlert(
        board_id=board_id, shortfall=shortfall, recipient_ids=admin_ids, owner_undeliverable=True
    )


def boards_needing_attention(session: Session) -> list[BoardAlert]:
    """Every published, underfull board's alert -- muted boards excluded.

    Unpublished boards never appear here: an unpublished board is not on a
    wall, so it cannot be short on one (Open-2).
    """
    boards = session.execute(
        select(Board.id, Board.items_per_page, Board.profile_id).where(Board.is_published.is_(True))
    ).all()
    renderable = _renderable_counts(session, [row.id for row in boards])
    admin_ids = _active_admin_ids(session)

    alerts: list[BoardAlert] = []
    for board_id, items_per_page, profile_id in boards:
        count = renderable.get(board_id, 0)
        if count >= items_per_page:
            continue
        alert = _alert_for(
            session,
            board_id=board_id,
            profile_id=profile_id,
            shortfall=items_per_page - count,
            admin_ids=admin_ids,
        )
        if alert is not None:
            alerts.append(alert)
    return alerts


def owner_undeliverable_map(session: Session, profile_ids: list[int]) -> dict[int, bool]:
    """profile_id -> whether ITS owner cannot receive a digest-channel
    notification right now. Independent of `board_alerts_enabled`: a muted
    owner is still a REACHABLE owner, so a mute must not paint the board as
    suppressed. Batched -- one query for however many boards are being
    listed, not one per board (same shape as `_renderable_counts` above and
    `_approved_counts` in `web/api/boards.py`). A profile whose owner cannot
    be resolved at all counts as undeliverable, same as a missing
    Subscription row does in `_alert_for`: fail toward visibility, not
    silence.
    """
    if not profile_ids:
        return {}
    owner_by_profile = dict(
        session.execute(
            select(Profile.id, Profile.created_by).where(Profile.id.in_(profile_ids))
        ).all()
    )
    owner_ids = [oid for oid in owner_by_profile.values() if oid is not None]
    deliverable_owner_ids = (
        set(
            session.execute(
                select(User.id).where(User.id.in_(owner_ids), _deliverable_clause())
            ).scalars()
        )
        if owner_ids
        else set()
    )
    return {
        profile_id: owner_by_profile.get(profile_id) not in deliverable_owner_ids
        for profile_id in profile_ids
    }
