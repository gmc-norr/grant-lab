from fastapi.testclient import TestClient

from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.auth.rate_limit import SlidingWindowLimiter
from call_monitor.db.models import User
from call_monitor.main import create_app


def _mk_app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    sender = InMemoryEmailSender()
    app.state.email_sender = sender
    # generous limits so shared tests don't interfere
    app.state.email_limiter = SlidingWindowLimiter(max_requests=100, window_seconds=60)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=100, window_seconds=60)
    return app, sender


def test_login_form_renders(monkeypatch, pg_url):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 200
    assert "email" in r.text.lower()


def test_login_post_sends_email_for_allowed_domain(monkeypatch, pg_url, pg_session_factory):
    app, sender = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "alice@ki.se"}, follow_redirects=False)
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert len(sender.sent) == 1
    assert sender.sent[0].to == "alice@ki.se"
    assert "/login/verify?token=" in sender.sent[0].text_body


def test_login_post_creates_user_on_first_request(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        client.post("/login", data={"email": "brand-new@ki.se"})
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="brand-new@ki.se").one()
        assert u.name is None
        assert u.is_admin is False


def test_login_post_silently_drops_disallowed_domain(monkeypatch, pg_url, pg_session_factory):
    app, sender = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "intruder@example.com"})
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert sender.sent == []


def test_login_post_is_rate_limited_per_email(monkeypatch, pg_url):
    app, sender = _mk_app(monkeypatch, pg_url)
    app.state.email_limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    with TestClient(app) as client:
        for _ in range(5):
            r = client.post("/login", data={"email": "alice@ki.se"})
            assert r.status_code == 200
    assert len(sender.sent) == 2
