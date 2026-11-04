"""D9: tell a board's profile owner when it has gone underfull.

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
`owner_undeliverable_map` (below) asserts the SAME row for the SAME reason --
see its own docstring (Task 8 review, Important 2).

THE MUTE SWITCH is `Subscription.board_alerts_enabled` (Open-1), never
`digest_enabled` -- muting the weekly email summary must not also mute the
alert that a public corridor screen has gone short.

THE ESCALATION (Open-3): when the owner is undeliverable --
`User.status != 'active'` OR `digest_disabled_at IS NOT NULL`, the same two
conditions `digest/send.py:deliverable_clause` already filters the weekly
digest on -- the recipients become every active admin instead, and
`owner_undeliverable` is True so `web/api/boards.py` can surface the
suppression on the board detail screen (half 2 of Open-3: "a delivery the
system knows it suppressed is the one case where an in-app surface is not
optional").

DELIVERABILITY BEFORE MUTE (Task 8 review, Important 5): `_alert_for` checks
whether the owner is reachable BEFORE it checks whether they muted board
alerts. `board_alerts_enabled` lives on `subscriptions(user_id, profile_id)`
-- it is a per-user preference meaning "do not email ME about this profile".
It cannot speak for admins, and an owner who is undeliverable is not
receiving anything anyway, so honouring their mailbox preference in that case
costs nothing and would only silence a public screen for free. So: owner
undeliverable -> escalate to admins REGARDLESS of `board_alerts_enabled`;
owner deliverable AND muted -> no alert.

ESCALATION RECIPIENTS ARE `deliverable_clause`-FILTERED ADMINS, not merely
"active" ones (Task 8 review, Important 4): the brief says "every active
admin", but `deliverable_clause()` (status AND digest_disabled_at) is the
exact predicate this module exists to single-source, and a bounce-disabled
admin receiving an escalation that exists BECAUSE mail bounces would defeat
the escalation's own purpose. This deliberately narrows the brief's literal
"active admin" to "an admin who can actually receive it".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape as _escape_html

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.db.models import Board, BoardItem, Call, Profile, Subscription, User
from grant_lab.digest.send import deliverable_clause
from grant_lab.email.client import EmailClient, EmailTransportError

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoardAlert:
    board_id: int
    shortfall: int  # items_per_page minus renderable items
    recipient_ids: list[int]  # the owner, or the escalation set
    owner_undeliverable: bool  # Open-3 half 2: shown in-app


@dataclass
class BoardAlertSummary:
    """Mirrors `digest.send.DigestRunSummary`'s shape: a plain counter
    dataclass a caller logs after the batch, not a per-recipient log of its
    own."""

    alerts_found: int = 0
    messages_sent: int = 0
    # Task 8 review (round 2), Important 2: a hard bounce -- `SendResult(ok=
    # False, bounce_reason=...)`, the shape `ResendClient`/`SMTPClient` use to
    # report an address-level permanent failure BY RETURN, not by raise --
    # counted separately from `messages_sent`. Mirrors
    # `DigestRunSummary.bounces` in shape only: a board alert must NOT touch
    # `User.digest_bounce_count` or `digest_disabled_at` -- that is the weekly
    # digest's own bounce-disable mechanism (`digest/send.py:183-201`) and a
    # board alert tripping it would auto-disable a user's WEEKLY DIGEST over
    # an address failure that has nothing to do with the digest.
    bounces: int = 0
    transport_errors: int = 0
    # Minor 6: an alert whose recipient_ids is empty (no deliverable admin to
    # escalate to) sends nothing. Counted here so that case is visible in the
    # tick log instead of looking identical to "nothing needed sending".
    no_recipients: int = 0


def _active_admin_ids(session: Session) -> list[int]:
    """Every admin who can actually receive an escalation email, id-ordered
    for a deterministic recipient list. Uses `deliverable_clause()` (status
    AND digest_disabled_at) rather than `status == 'active'` alone -- see the
    module docstring's "ESCALATION RECIPIENTS" section (Important 4)."""
    return list(
        session.execute(
            select(User.id).where(User.is_admin.is_(True), deliverable_clause()).order_by(User.id)
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
    (reachable) owner has muted board alerts. `owner_id` is resolved from
    `profiles.created_by` -- never `boards.created_by` -- before anything
    else, so the Subscription lookup below is a targeted assertion on THAT
    person's row, not an iteration a missing row could silently fall through.

    Order of checks, deliberately (Important 5): missing-subscription, THEN
    deliverability, THEN mute. Deliverability is checked before the mute so
    an undeliverable owner escalates to admins even if they had also muted
    board alerts on their way out -- see the module docstring's
    "DELIVERABILITY BEFORE MUTE" section for why.

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

    owner_reachable = (
        session.scalar(select(User.id).where(User.id == owner_id, deliverable_clause())) is not None
    )
    if not owner_reachable:
        # Escalate regardless of `sub.board_alerts_enabled` -- see
        # "DELIVERABILITY BEFORE MUTE" above. An owner who cannot receive
        # anything is not made "reachable" by having also muted the alert.
        return BoardAlert(
            board_id=board_id,
            shortfall=shortfall,
            recipient_ids=admin_ids,
            owner_undeliverable=True,
        )

    if not sub.board_alerts_enabled:
        return None

    return BoardAlert(
        board_id=board_id,
        shortfall=shortfall,
        recipient_ids=[owner_id],
        owner_undeliverable=False,
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
    """profile_id -> whether a board alert on THIS profile would currently be
    escalated away from its owner. Matches `_alert_for`'s own truth table
    exactly (Task 8 review, Important 2): a profile counts as undeliverable
    when its owner cannot be resolved, OR the owner has no Subscription row
    for this profile (D10 -- the same convention-not-constraint `_alert_for`
    asserts rather than trusts), OR the owner fails `deliverable_clause()`.

    Independent of `board_alerts_enabled` in all three cases: a muted owner
    is still a REACHABLE owner (a mute does not make an owner unreachable),
    so a mute must not paint the board as suppressed on the detail screen --
    that is a genuinely different question from "can we reach them at all",
    which is what this field promises to answer for a human reading the
    board detail screen.

    Batched -- one query for however many boards are being listed, not one
    per board (same shape as `_renderable_counts` above and
    `_approved_counts` in `web/api/boards.py`).
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
                select(User.id).where(User.id.in_(owner_ids), deliverable_clause())
            ).scalars()
        )
        if owner_ids
        else set()
    )
    # D10: a profile whose owner has no Subscription row for THIS profile is
    # reported the same way _alert_for reports it -- undeliverable, not
    # silently assumed reachable. Matched on the (owner_id, profile_id) PAIR,
    # not "does some Subscription row exist for this profile_id", because the
    # row is only meaningful when it belongs to the resolved owner.
    subscribed_pairs = (
        {
            (row.user_id, row.profile_id)
            for row in session.execute(
                select(Subscription.user_id, Subscription.profile_id).where(
                    Subscription.profile_id.in_(profile_ids),
                    Subscription.user_id.in_(owner_ids),
                )
            ).all()
        }
        if owner_ids
        else set()
    )
    result: dict[int, bool] = {}
    for profile_id in profile_ids:
        owner_id = owner_by_profile.get(profile_id)
        result[profile_id] = (
            owner_id is None
            or (owner_id, profile_id) not in subscribed_pairs
            or owner_id not in deliverable_owner_ids
        )
    return result


def send_board_alerts(*, session_factory, email_client: EmailClient, settings) -> BoardAlertSummary:
    """Send one message per `BoardAlert.recipient_ids` entry (Task 8 review,
    Important 1). Runs on the SAME schedule as `run_digest` -- called from
    the existing `_digest_tick` closure in `scheduler.py`; no new job, lock
    or route.

    Mirrors `run_digest`'s (`digest/send.py`) per-recipient try/except so one
    bad address or one transport blip does not stop the rest of the batch,
    and its logging-event-per-outcome convention -- transport error, hard
    bounce (`SendResult(ok=False, ...)`, counted but never raising
    `digest_bounce_count`, Task 8 review round 2 Important 2), success. Unlike
    `run_digest`, this has no idempotency table to consult: `_digest_tick`
    only fires weekly, so an owner or admin is told about a still-underfull
    board again next week, which is the intended behaviour for an ongoing
    shortfall, not a defect.

    `settings` is duck-typed the same way `run_digest`'s is -- BOTH
    `.digest_dry_run` and `.admin_dashboard_url` are read here (Task 8 review
    round 2, Important 1: round 1's version read only the URL and sent real
    mail regardless of `DIGEST_DRY_RUN`).
    """
    summary = BoardAlertSummary()
    with session_factory() as session:
        alerts = boards_needing_attention(session)
        summary.alerts_found = len(alerts)
        if not alerts:
            return summary
        board_titles = dict(
            session.execute(
                select(Board.id, Board.title).where(Board.id.in_([a.board_id for a in alerts]))
            ).all()
        )
        recipient_ids = {rid for a in alerts for rid in a.recipient_ids}
        recipient_emails = (
            dict(
                session.execute(select(User.id, User.email).where(User.id.in_(recipient_ids))).all()
            )
            if recipient_ids
            else {}
        )

    for alert in alerts:
        if not alert.recipient_ids:
            # Minor 6: nobody to tell -- an undeliverable owner AND zero
            # deliverable admins. Count and log it distinctly rather than
            # letting it look like a delivered alert.
            summary.no_recipients += 1
            _logger.warning(
                "board_alert.no_recipients",
                extra={"event": "board_alert_no_recipients", "board_id": alert.board_id},
            )
            continue

        title = board_titles.get(alert.board_id, f"board {alert.board_id}")
        # Final security review, CRITICAL: `title` is free text any board
        # editor can set (`BoardCreateIn.title` / `BoardSettingsIn.title`
        # enforce only `min_length=1`), and it used to go straight into the
        # email `subject` unfiltered. `SMTPClient.send` (email/client.py)
        # does `msg["Subject"] = subject`, and `EmailMessage` raises
        # `ValueError: Header values may not contain linefeed or carriage
        # return characters` for a subject containing "\r" or "\n" --
        # measured on Python 3.12. Not a header-injection risk (the stdlib
        # refuses rather than folding the extra header in), but a
        # SILENT-FOREVER one: the `except Exception` below catches that
        # ValueError, logs `board_alert.recipient_failed`, and moves on --
        # so a board owner (or, on the escalation path, EVERY deliverable
        # admin) never receives the alert, every week, for as long as the
        # title contains a newline. `digest/render.py:65`'s `build_subject`
        # already strips CR/LF for exactly this reason -- this module's own
        # docstring claims to mirror `run_digest`'s conventions; this line
        # is the one place it hadn't. See
        # `test_a_newline_in_the_board_title_does_not_silently_swallow_the_alert`.
        safe_title = title.replace("\r", " ").replace("\n", " ")
        subject = f'Corridor board "{safe_title}" is short {alert.shortfall} item(s)'
        sentence = (
            f'The corridor board "{title}" currently has {alert.shortfall} fewer '
            f"approved, still-open item(s) than its configured page size."
        )
        text = f"{sentence}\n\n{settings.admin_dashboard_url}"
        # Important 3 (Task 8 review round 2): `title` is free text any board
        # editor can set (`Board.title` is `Text`; `web/api/boards.py`
        # enforces only `min_length=1`) and on the escalation path this HTML
        # reaches every deliverable admin -- an unescaped `<a href>` in a
        # title is an admin-wide phishing vector. `digest/render.py` sets
        # `autoescape=True` on its Jinja env EXPLICITLY for exactly this
        # reason (its own comment explains why `select_autoescape()` would
        # not have caught it for a `.jinja` filename); this module builds its
        # HTML by hand, so it escapes by hand. The plain-text alternative
        # stays UNESCAPED -- escaping it would show a human a literal
        # "&amp;". Two separate `<p>` tags (not one `\n\n`-joined block) so
        # the URL doesn't collapse into a run-on sentence, and the URL is a
        # live link the way `digest.html.jinja` always links
        # `dashboard_url`.
        html_sentence = (
            f'The corridor board "{_escape_html(title)}" currently has {alert.shortfall} '
            f"fewer approved, still-open item(s) than its configured page size."
        )
        html = (
            f"<p>{html_sentence}</p>"
            f'<p><a href="{settings.admin_dashboard_url}">{settings.admin_dashboard_url}</a></p>'
        )

        for uid in alert.recipient_ids:
            to = recipient_emails.get(uid)
            if to is None:
                # Recipient id resolved to no live User row between
                # boards_needing_attention() and here -- log and move on,
                # the same per-recipient isolation run_digest gives a
                # send failure.
                _logger.warning(
                    "board_alert.recipient_unresolved",
                    extra={
                        "event": "board_alert_recipient_unresolved",
                        "board_id": alert.board_id,
                        "user_id": uid,
                    },
                )
                continue

            if settings.digest_dry_run:
                # Important 1 (Task 8 review round 2): honour the SAME flag
                # `run_digest` does (`digest/send.py:122`), the same way --
                # skip the send, log an equivalent event, do not count it as
                # sent. Round 1 read only `.admin_dashboard_url` off
                # `settings` and called `email_client.send` unconditionally,
                # so a dev/staging box with `DIGEST_DRY_RUN=true` and a
                # configured transport would send real board-alert mail.
                _logger.info(
                    "board_alert.dry_run",
                    extra={
                        "event": "board_alert_dry_run",
                        "board_id": alert.board_id,
                        "user_id": uid,
                        "to": to,
                        "subject": subject,
                    },
                )
                continue

            try:
                result = email_client.send(to=to, subject=subject, html=html, text=text)
            except EmailTransportError as e:
                summary.transport_errors += 1
                _logger.warning(
                    "board_alert.transport_error",
                    extra={
                        "event": "board_alert_transport_error",
                        "board_id": alert.board_id,
                        "user_id": uid,
                        "error": str(e),
                    },
                )
                continue
            except Exception as e:  # noqa: BLE001
                _logger.exception(
                    "board_alert.recipient_failed",
                    extra={
                        "event": "board_alert_recipient_failed",
                        "board_id": alert.board_id,
                        "user_id": uid,
                        "error": str(e),
                    },
                )
                continue

            if not result.ok:
                # Important 2 (Task 8 review round 2): a hard bounce is
                # reported BY RETURN, not by raise -- MINOR (final security
                # review): re-checked against the file, which had moved;
                # the Resend return is `email/client.py:128-131`
                # (`return SendResult(ok=False, bounce_reason=...)` inside
                # `ResendClient.send`) and the SMTP return is `:187`
                # (`return SendResult(ok=False, bounce_reason=f"refused:
                # {details}")` inside `SMTPClient.send`), not the
                # `:118-122`/`:170-175` this comment previously cited. So
                # round 1's discarded `SendResult` counted every bounce as a delivered
                # alert. Count it distinctly and deliberately do NOT touch
                # `User.digest_bounce_count` / `digest_disabled_at` -- that is
                # the weekly digest's own bounce-disable mechanism
                # (`digest/send.py:183-201`) and a board alert must not trip
                # it.
                summary.bounces += 1
                _logger.warning(
                    "board_alert.bounce",
                    extra={
                        "event": "board_alert_bounce",
                        "board_id": alert.board_id,
                        "user_id": uid,
                        "reason": result.bounce_reason,
                    },
                )
                continue

            summary.messages_sent += 1

    return summary
