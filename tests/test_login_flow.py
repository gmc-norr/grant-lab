from fastapi.testclient import TestClient

from call_manager.auth.email import InMemoryEmailSender
from call_manager.auth.rate_limit import SlidingWindowLimiter
from call_manager.db.models import User
from call_manager.main import create_app


def test_full_login_flow(monkeypatch, pg_url, pg_session_factory):
    import html
    import re

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")

    # Magic-link is login-only: it issues a link only for an existing ACTIVE
    # account. Seed one before requesting the link.
    with pg_session_factory() as s:
        s.add(User(status="active", email="alice@ki.se"))
        s.commit()

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
        path = link[link.index("/login/verify") :]

        # 2. Click the emailed link → scanner-proof interstitial (GET consumes
        #    nothing). Read the token out of the rendered confirm form.
        interstitial = client.get(path, follow_redirects=False)
        assert interstitial.status_code == 200
        assert "confirm sign-in" in interstitial.text.lower()
        token = html.unescape(
            re.search(r'name="token"[^>]*value="([^"]*)"', interstitial.text).group(1)
        )

        # 3. Confirm sign-in (the human's POST) → redirected, cookie set
        r = client.post("/login/verify", data={"token": token}, follow_redirects=False)
        assert r.status_code == 303
        assert "session" in client.cookies

        # 4. /me works
        r = client.get("/me")
        assert r.status_code == 200
        assert "alice@ki.se" in r.text

        # 5. Replay of the same token is rejected (single-use), HTTP 400
        r = client.post("/login/verify", data={"token": token}, follow_redirects=False)
        assert r.status_code == 400
