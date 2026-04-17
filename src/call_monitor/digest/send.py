from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import distinct, select

from call_monitor.db.models import DigestRun, Subscription, User
from call_monitor.digest.assemble import assemble_for_user
from call_monitor.digest.render import build_subject, render_html, render_text
from call_monitor.email.client import EmailClient, EmailTransportError

_logger = logging.getLogger(__name__)

BOUNCE_DISABLE_THRESHOLD = 3
_TZ = ZoneInfo("Europe/Stockholm")


@dataclass
class DigestRunSummary:
    users_processed: int = 0
    emails_sent: int = 0
    bounces: int = 0
    transport_errors: int = 0
    users_disabled: int = 0


def _week_start(now: datetime) -> datetime:
    """Stockholm-week start (Monday 00:00 Europe/Stockholm) for the week
    containing `now`.

    Why Stockholm and not UTC: the cadence is "Monday 07:00 Europe/Stockholm"
    (spec line 409). If we computed week_start in UTC, a manual run between
    UTC Sunday 22:00 and UTC Monday 00:00 (= Stockholm Mon 00:00–02:00) would
    fall in the *prior* UTC week and silently re-send a digest the user
    already received at the previous Stockholm-Monday 07:00. Stockholm-week
    aligns the idempotency window with the user's perception of "this week"."""
    local = now.astimezone(_TZ)
    monday_local = local - timedelta(days=local.isoweekday() - 1)
    monday_local = monday_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return monday_local.astimezone(now.tzinfo or _TZ)


def run_digest(
    *,
    session_factory,
    email_client: EmailClient,
    settings,
    now: datetime,
) -> DigestRunSummary:
    """Iterate all subscribed users, assemble + send a digest each.

    `settings` may be the real Settings or any object with `.digest_dry_run`
    and `.admin_dashboard_url` attributes (duck-typed for tests).

    Per-recipient try/except so one bounce doesn't stop the batch.
    """
    summary = DigestRunSummary()
    week_start = _week_start(now)

    # Find users who have at least one subscription AND are not auto-disabled.
    # Skipping disabled users at SQL keeps the `users_processed` metric meaningful
    # — assemble_for_user would also reject them, but we'd inflate the count.
    with session_factory() as session:
        candidate_user_ids = session.execute(
            select(distinct(Subscription.user_id))
            .join(User, User.id == Subscription.user_id)
            .where(User.digest_disabled_at.is_(None))
        ).scalars().all()

    for uid in candidate_user_ids:
        summary.users_processed += 1
        with session_factory() as session:
            # Skip if already sent this week (idempotency)
            existing = session.execute(
                select(DigestRun.id).where(
                    DigestRun.user_id == uid,
                    DigestRun.sent_at >= week_start,
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue

            try:
                payload = assemble_for_user(session, user_id=uid, now=now)
                if payload is None:
                    continue

                html = render_html(payload, dashboard_url=settings.admin_dashboard_url)
                text = render_text(payload, dashboard_url=settings.admin_dashboard_url)
                subject = build_subject(payload)

                if settings.digest_dry_run:
                    _logger.info(
                        "digest.dry_run",
                        extra={
                            "event": "digest_dry_run",
                            "user_id": uid,
                            "to": payload.user_email,
                            "subject": subject,
                            "n_calls": payload.total_calls,
                        },
                    )
                    continue

                try:
                    result = email_client.send(
                        to=payload.user_email, subject=subject, html=html, text=text
                    )
                except EmailTransportError as e:
                    summary.transport_errors += 1
                    _logger.warning(
                        "digest.transport_error",
                        extra={"event": "digest_transport_error", "user_id": uid, "error": str(e)},
                    )
                    continue

                user = session.get(User, uid)
                if not result.ok:
                    summary.bounces += 1
                    user.digest_bounce_count = (user.digest_bounce_count or 0) + 1
                    _logger.warning(
                        "digest.bounce",
                        extra={
                            "event": "digest_bounce",
                            "user_id": uid,
                            "bounce_count": user.digest_bounce_count,
                            "reason": result.bounce_reason,
                        },
                    )
                    if user.digest_bounce_count >= BOUNCE_DISABLE_THRESHOLD:
                        user.digest_disabled_at = now
                        summary.users_disabled += 1
                    session.commit()
                    continue

                # Success
                user.digest_bounce_count = 0
                session.add(DigestRun(
                    user_id=uid,
                    included_call_ids=[e.call_id for sec in payload.sections for e in sec.entries],
                    sent_at=now,
                ))
                session.commit()
                summary.emails_sent += 1
            except Exception as e:  # noqa: BLE001
                _logger.exception(
                    "digest.user_failed",
                    extra={"event": "digest_user_failed", "user_id": uid, "error": str(e)},
                )
                # Continue with next user — one bad profile mustn't kill the batch
                continue

    return summary
