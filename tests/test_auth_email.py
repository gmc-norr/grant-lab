import logging

from call_monitor.auth.email import ConsoleEmailSender, InMemoryEmailSender


def test_in_memory_sender_records_messages():
    sender = InMemoryEmailSender()
    sender.send(
        to="alice@ki.se",
        subject="Your login link",
        text_body="Click: https://x/verify?token=abc",
    )
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert msg.to == "alice@ki.se"
    assert msg.subject == "Your login link"
    assert "https://x/verify?token=abc" in msg.text_body


def test_console_sender_logs_full_body(caplog):
    sender = ConsoleEmailSender()
    with caplog.at_level(logging.INFO, logger="call_monitor.auth.email"):
        sender.send(
            to="alice@ki.se",
            subject="Your login link",
            text_body="Click: https://x/verify?token=abc",
        )
    assert any(
        "https://x/verify?token=abc" in r.getMessage()
        or "https://x/verify?token=abc" in str(getattr(r, "body", ""))
        for r in caplog.records
    )
