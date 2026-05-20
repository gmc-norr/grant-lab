from call_manager.auth.email import EmailSendFailed, InMemoryEmailSender
from call_manager.ingestion.alerts import ConsecutiveFailureTracker


class _FailingSender:
    """Test double: every send() raises EmailSendFailed.

    Used to exercise the alerter's behavior when the email provider is
    down at the moment we'd normally fire the threshold-crossing alert."""

    def __init__(self):
        self.attempts: list[tuple[str, str]] = []

    def send(self, *, to: str, subject: str, text_body: str) -> None:
        self.attempts.append((to, subject))
        raise EmailSendFailed("simulated provider outage")


def test_threshold_not_yet_reached():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    t.record_failure("eu_portal", error="oops")
    t.record_failure("eu_portal", error="oops")
    assert sender.sent == []


def test_threshold_reached_emits_one_email():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    for _ in range(3):
        t.record_failure("eu_portal", error="boom")
    assert len(sender.sent) == 1
    assert "eu_portal" in sender.sent[0].subject
    assert "boom" in sender.sent[0].text_body


def test_continued_failure_does_not_re_alert():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    for _ in range(5):
        t.record_failure("eu_portal", error="boom")
    assert len(sender.sent) == 1


def test_success_resets_counter_and_re_arms_alerts():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    for _ in range(3):
        t.record_failure("eu_portal", error="boom")
    t.record_success("eu_portal")
    for _ in range(3):
        t.record_failure("eu_portal", error="other")
    assert len(sender.sent) == 2  # one for each separate streak


def test_per_source_independence():
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email="a@ki.se")
    t.record_failure("eu_portal")
    t.record_failure("eu_portal")
    t.record_failure("vinnova")
    t.record_failure("vinnova")
    assert sender.sent == []  # neither has crossed threshold
    t.record_failure("eu_portal")
    assert len(sender.sent) == 1
    assert "eu_portal" in sender.sent[0].subject


def test_no_admin_email_skips_send():
    """If no admin is configured, just track silently."""
    sender = InMemoryEmailSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=sender, admin_email=None)
    for _ in range(5):
        t.record_failure("eu_portal", error="boom")
    assert sender.sent == []


def test_send_failure_does_not_suppress_future_alerts():
    """If the email provider is also down at the moment we'd cross the
    threshold, the alerter must NOT mark the source as 'already alerted'
    — otherwise one transient mail outage would suppress every future
    alert for this streak, masking the real ongoing ingestion outage.

    Concretely: 3 failures cross threshold → send attempted (raises) →
    alerted stays False → 4th failure retries the send.
    """
    failing = _FailingSender()
    t = ConsecutiveFailureTracker(threshold=3, sender=failing, admin_email="a@ki.se")
    for _ in range(3):
        t.record_failure("eu_portal", error="boom")
    assert len(failing.attempts) == 1  # threshold-crossing fired

    # Next failure: should re-attempt (not suppressed).
    t.record_failure("eu_portal", error="boom")
    assert len(failing.attempts) == 2

    # And keep retrying as long as the streak continues.
    t.record_failure("eu_portal", error="boom")
    assert len(failing.attempts) == 3
