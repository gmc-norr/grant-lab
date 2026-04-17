from fastapi.testclient import TestClient

from call_monitor.auth.email import InMemoryEmailSender
from call_monitor.auth.rate_limit import SlidingWindowLimiter
from call_monitor.main import create_app


def test_full_login_flow(monkeypatch, pg_url, pg_session_factory):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")

    app = create_app()
    sender = InMemoryEmailSender()
    app.state.email_sender = sender
    app.state.email_limiter = SlidingWindowLimiter(max_requests=10, window_seconds=60)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=100, window_seconds=60)

    with TestClient(app) as client:
        # 1. Request magic link
        r = client.post("/login", data={"email": "alice@ki.se"})
        assert r.status_code == 200
        assert len(sender.sent) == 1
        body = sender.sent[0].text_body
        link = [line for line in body.splitlines() if "login/verify" in line][0].strip()
        # Strip host so TestClient can use it
        path = link[link.index("/login/verify"):]

        # 2. Click link → redirected, cookie set
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303
        assert "session" in client.cookies

        # 3. /me works
        r = client.get("/me")
        assert r.status_code == 200
        assert "alice@ki.se" in r.text

        # 4. Replay of the same link is rejected
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 400
