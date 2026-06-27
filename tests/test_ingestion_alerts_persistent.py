"""Tests for the persisted-streak and stale-fetch backstop alerter.

Covers:
  - Streak survives a simulated restart (fresh tracker instance reads DB state).
  - Backstop: source with last_fetched_at older than threshold triggers an alert
    even with consecutive_failures=0.
  - Success resets consecutive_failures to 0 in the DB.
  - continued failures after threshold still don't re-alert (alerted guard).
  - per-source independence persisted to DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from call_manager.auth.email import InMemoryEmailSender
from call_manager.db.models import Source
from call_manager.ingestion.alerts import ConsecutiveFailureTracker, check_stale_sources

# ---------------------------------------------------------------------------
# Helper: read Source from DB
# ---------------------------------------------------------------------------


def _get_source(session_factory, slug: str) -> Source:
    with session_factory() as s:
        return s.execute(select(Source).where(Source.slug == slug)).scalar_one()


# ---------------------------------------------------------------------------
# Streak-persistence tests
# ---------------------------------------------------------------------------


def test_failure_streak_persisted_to_db(pg_session_factory):
    """record_failure increments consecutive_failures on the DB row."""
    sender = InMemoryEmailSender()
    tracker = ConsecutiveFailureTracker(
        threshold=3,
        sender=sender,
        admin_email="root@ki.se",
        session_factory=pg_session_factory,
    )

    tracker.record_failure("vinnova", error="net blip")
    tracker.record_failure("vinnova", error="net blip")

    src = _get_source(pg_session_factory, "vinnova")
    assert src.consecutive_failures == 2
    assert src.last_alerted_at is None  # threshold not yet reached
    assert sender.sent == []


def test_streak_survives_simulated_restart(pg_session_factory):
    """A fresh tracker instance (simulated restart) reads DB state and alerts
    on the next failure when the threshold is reached."""
    sender_before = InMemoryEmailSender()
    tracker_before = ConsecutiveFailureTracker(
        threshold=3,
        sender=sender_before,
        admin_email="root@ki.se",
        session_factory=pg_session_factory,
    )
    # Record 2 failures — persist count=2, no alert yet.
    tracker_before.record_failure("vinnova", error="boom")
    tracker_before.record_failure("vinnova", error="boom")

    src = _get_source(pg_session_factory, "vinnova")
    assert src.consecutive_failures == 2

    # --- Simulated restart: brand-new tracker with no shared memory ---
    sender_after = InMemoryEmailSender()
    tracker_after = ConsecutiveFailureTracker(
        threshold=3,
        sender=sender_after,
        admin_email="root@ki.se",
        session_factory=pg_session_factory,
    )
    # Fresh tracker must read from DB → count becomes 3 → alert fires.
    tracker_after.record_failure("vinnova", error="still broken")

    assert len(sender_after.sent) == 1, "alert must fire at threshold 3 after restart"
    assert "vinnova" in sender_after.sent[0].subject

    src = _get_source(pg_session_factory, "vinnova")
    assert src.consecutive_failures == 3
    assert src.last_alerted_at is not None


def test_success_resets_consecutive_failures_in_db(pg_session_factory):
    """record_success persists consecutive_failures=0 to the DB row."""
    sender = InMemoryEmailSender()
    tracker = ConsecutiveFailureTracker(
        threshold=3,
        sender=sender,
        admin_email="root@ki.se",
        session_factory=pg_session_factory,
    )

    tracker.record_failure("vinnova", error="boom")
    tracker.record_failure("vinnova", error="boom")
    tracker.record_success("vinnova")

    src = _get_source(pg_session_factory, "vinnova")
    assert src.consecutive_failures == 0


def test_alert_not_resent_on_continued_failure_after_restart(pg_session_factory):
    """After an alert is sent, a subsequent tracker restart must NOT re-alert
    on the same streak (last_alerted_at guards it)."""
    sender1 = InMemoryEmailSender()
    tracker1 = ConsecutiveFailureTracker(
        threshold=3,
        sender=sender1,
        admin_email="root@ki.se",
        session_factory=pg_session_factory,
    )
    for _ in range(3):
        tracker1.record_failure("vinnova", error="boom")
    assert len(sender1.sent) == 1

    # Simulated restart after alert was sent.
    sender2 = InMemoryEmailSender()
    tracker2 = ConsecutiveFailureTracker(
        threshold=3,
        sender=sender2,
        admin_email="root@ki.se",
        session_factory=pg_session_factory,
    )
    tracker2.record_failure("vinnova", error="still broken")
    assert sender2.sent == [], "must NOT re-alert on same streak after restart"


def test_per_source_independence_persisted(pg_session_factory):
    """Streak counters are independent per source slug."""
    sender = InMemoryEmailSender()
    tracker = ConsecutiveFailureTracker(
        threshold=3,
        sender=sender,
        admin_email="root@ki.se",
        session_factory=pg_session_factory,
    )

    tracker.record_failure("vinnova", error="boom")
    tracker.record_failure("vinnova", error="boom")
    tracker.record_failure("eu_portal", error="timeout")

    vinnova = _get_source(pg_session_factory, "vinnova")
    eu = _get_source(pg_session_factory, "eu_portal")
    assert vinnova.consecutive_failures == 2
    assert eu.consecutive_failures == 1
    assert sender.sent == []

    tracker.record_failure("vinnova", error="boom")
    assert len(sender.sent) == 1
    assert "vinnova" in sender.sent[0].subject

    # eu_portal still has count=1, not alerted.
    eu = _get_source(pg_session_factory, "eu_portal")
    assert eu.consecutive_failures == 1


# ---------------------------------------------------------------------------
# Existing in-memory tests still pass without session_factory
# ---------------------------------------------------------------------------


def test_in_memory_only_still_works():
    """Tracker without session_factory behaves exactly as before — the existing
    in-memory tests cover this, this is a smoke-test of the no-DB path."""
    sender = InMemoryEmailSender()
    tracker = ConsecutiveFailureTracker(
        threshold=3,
        sender=sender,
        admin_email="root@ki.se",
        # No session_factory — pure in-memory
    )
    for _ in range(3):
        tracker.record_failure("eu_portal", error="oops")
    assert len(sender.sent) == 1


# ---------------------------------------------------------------------------
# Stale-source backstop tests
# ---------------------------------------------------------------------------


def test_stale_source_triggers_alert(pg_session_factory):
    """A source with last_fetched_at older than stale_days triggers an alert
    even when consecutive_failures == 0."""
    # Manually set last_fetched_at to 5 days ago (threshold is 3 days).
    five_days_ago = datetime.now(UTC) - timedelta(days=5)
    with pg_session_factory() as s:
        s.execute(
            Source.__table__.update()
            .where(Source.slug == "vinnova")
            .values(last_fetched_at=five_days_ago)
        )
        s.commit()

    sender = InMemoryEmailSender()
    check_stale_sources(
        session_factory=pg_session_factory,
        sender=sender,
        admin_email="root@ki.se",
        stale_days=3,
    )

    assert len(sender.sent) == 1
    assert "vinnova" in sender.sent[0].text_body


def test_stale_source_never_fetched_triggers_alert(pg_session_factory):
    """A source with last_fetched_at == NULL (never fetched) triggers an alert."""
    # last_fetched_at is NULL by default after the fixture seeds the row.
    sender = InMemoryEmailSender()
    check_stale_sources(
        session_factory=pg_session_factory,
        sender=sender,
        admin_email="root@ki.se",
        stale_days=3,
    )

    assert len(sender.sent) == 1
    assert "NEVER" in sender.sent[0].text_body


def test_recently_fetched_source_no_alert(pg_session_factory):
    """A source fetched within stale_days does not trigger an alert."""
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    with pg_session_factory() as s:
        # Set both sources to recently fetched.
        s.execute(
            Source.__table__.update().values(last_fetched_at=one_hour_ago)
        )
        s.commit()

    sender = InMemoryEmailSender()
    check_stale_sources(
        session_factory=pg_session_factory,
        sender=sender,
        admin_email="root@ki.se",
        stale_days=3,
    )

    assert sender.sent == []


def test_stale_check_no_admin_email_skips(pg_session_factory):
    """check_stale_sources is a no-op when admin_email is None."""
    five_days_ago = datetime.now(UTC) - timedelta(days=5)
    with pg_session_factory() as s:
        s.execute(
            Source.__table__.update()
            .where(Source.slug == "vinnova")
            .values(last_fetched_at=five_days_ago)
        )
        s.commit()

    sender = InMemoryEmailSender()
    check_stale_sources(
        session_factory=pg_session_factory,
        sender=sender,
        admin_email=None,
        stale_days=3,
    )

    assert sender.sent == []


def test_stale_check_zero_failures_independent_of_streak(pg_session_factory):
    """Stale backstop fires even when consecutive_failures == 0 — the two
    mechanisms are independent."""
    five_days_ago = datetime.now(UTC) - timedelta(days=5)
    with pg_session_factory() as s:
        s.execute(
            Source.__table__.update()
            .where(Source.slug == "eu_portal")
            .values(last_fetched_at=five_days_ago, consecutive_failures=0)
        )
        # vinnova: recently fetched, no alert expected for it.
        s.execute(
            Source.__table__.update()
            .where(Source.slug == "vinnova")
            .values(last_fetched_at=datetime.now(UTC) - timedelta(hours=1))
        )
        s.commit()

    sender = InMemoryEmailSender()
    check_stale_sources(
        session_factory=pg_session_factory,
        sender=sender,
        admin_email="root@ki.se",
        stale_days=3,
    )

    assert len(sender.sent) == 1
    assert "eu_portal" in sender.sent[0].text_body
    assert "vinnova" not in sender.sent[0].text_body


def test_stale_backstop_suppressed_within_cooldown(pg_session_factory):
    """A source that stays stale must NOT re-alert every day: the second
    same-day check_stale_sources call is suppressed because last_alerted_at was
    stamped on the first send (within the cooldown window)."""
    five_days_ago = datetime.now(UTC) - timedelta(days=5)
    with pg_session_factory() as s:
        s.execute(
            Source.__table__.update()
            .where(Source.slug == "vinnova")
            .values(last_fetched_at=five_days_ago)
        )
        # vinnova will alert; make eu_portal recent so only one row is stale.
        s.execute(
            Source.__table__.update()
            .where(Source.slug == "eu_portal")
            .values(last_fetched_at=datetime.now(UTC) - timedelta(hours=1))
        )
        s.commit()

    sender = InMemoryEmailSender()
    check_stale_sources(
        session_factory=pg_session_factory,
        sender=sender,
        admin_email="root@ki.se",
        stale_days=3,
    )
    assert len(sender.sent) == 1, "first stale check should alert"

    # last_alerted_at must have been stamped for the alerted source.
    src = _get_source(pg_session_factory, "vinnova")
    assert src.last_alerted_at is not None

    # Second call the same day: still stale, but suppressed by the cooldown.
    check_stale_sources(
        session_factory=pg_session_factory,
        sender=sender,
        admin_email="root@ki.se",
        stale_days=3,
    )
    assert len(sender.sent) == 1, "stale backstop must not re-send within cooldown"


def test_stale_backstop_re_alerts_after_cooldown(pg_session_factory):
    """Once the cooldown (the stale window) elapses with the source still stale,
    the backstop alerts again — it is a nag, not a one-shot."""
    five_days_ago = datetime.now(UTC) - timedelta(days=5)
    # last_alerted_at older than stale_days ⇒ cooldown elapsed ⇒ re-alert.
    long_ago = datetime.now(UTC) - timedelta(days=10)
    with pg_session_factory() as s:
        s.execute(
            Source.__table__.update()
            .where(Source.slug == "vinnova")
            .values(last_fetched_at=five_days_ago, last_alerted_at=long_ago)
        )
        s.execute(
            Source.__table__.update()
            .where(Source.slug == "eu_portal")
            .values(last_fetched_at=datetime.now(UTC) - timedelta(hours=1))
        )
        s.commit()

    sender = InMemoryEmailSender()
    check_stale_sources(
        session_factory=pg_session_factory,
        sender=sender,
        admin_email="root@ki.se",
        stale_days=3,
    )
    assert len(sender.sent) == 1, "cooldown elapsed ⇒ re-alert"
