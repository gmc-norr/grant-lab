from call_manager.auth.email import ConsoleEmailSender, NoOpEmailSender
from call_manager.main import create_app


def _env(monkeypatch, pg_url, base_url, secret):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": secret,
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": base_url,
    }.items():
        monkeypatch.setenv(k, v)


def test_prod_without_sender_uses_noop(monkeypatch, pg_url):
    # https + no RESEND/SMTP: previously refused to start; now NoOp (drops alerts,
    # no body logged) instead of console-logging alert contents in prod.
    _env(monkeypatch, pg_url, "https://app.example.org", "x" * 40)
    app = create_app()
    try:
        assert isinstance(app.state.email_sender, NoOpEmailSender)
    finally:
        app.state.engine.dispose()


def test_dev_without_sender_uses_console(monkeypatch, pg_url):
    _env(monkeypatch, pg_url, "http://testserver", "test-secret-at-least-16-chars")
    app = create_app()
    try:
        assert isinstance(app.state.email_sender, ConsoleEmailSender)
    finally:
        app.state.engine.dispose()
