from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.ingestion.alerts import ConsecutiveFailureTracker


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
