import contextlib
import logging

import pytest
from fastapi.testclient import TestClient

from call_manager.auth.email import EmailSendFailed, InMemoryEmailSender
from call_manager.auth.rate_limit import SlidingWindowLimiter
from call_manager.auth.tokens import issue_token
from call_manager.db.models import User
from call_manager.main import create_app


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


def test_login_post_is_rate_limited_per_email(monkeypatch, pg_url, pg_session_factory):
    app, sender = _mk_app(monkeypatch, pg_url)
    app.state.email_limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    with TestClient(app) as client:
        for _ in range(5):
            r = client.post("/login", data={"email": "alice@ki.se"})
            assert r.status_code == 200
    assert len(sender.sent) == 2


def test_verify_sets_session_cookie_and_redirects(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.post("/login/verify", data={"token": raw}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "session" in r.cookies


def test_verify_rejects_unknown_token(monkeypatch, pg_url):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login/verify", data={"token": "nope"}, follow_redirects=False)
    assert r.status_code == 400


def test_verify_rejects_replay(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        assert (
            client.post("/login/verify", data={"token": raw}, follow_redirects=False).status_code
            == 303
        )
        r = client.post("/login/verify", data={"token": raw}, follow_redirects=False)
    assert r.status_code == 400


def test_verify_survives_post_consumption_failure(monkeypatch, pg_url, pg_session_factory):
    """If anything between consume_token() and the handler's commit fails,
    the token must still be marked consumed. Otherwise the handler leaks
    its "return 400" / "raise" paths into a single-use-violation.

    We simulate this by monkey-patching User.last_login_at assignment to
    raise, which is the exact code path that could blow up post-consume.
    """
    from call_manager.auth.tokens import hash_token
    from call_manager.db.models import MagicLinkToken

    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    # Force any attempt to set last_login_at to explode.
    import call_manager.web.routes.auth as auth_module

    sentinel = RuntimeError("simulated failure post-consumption")

    class BoomDateTime:
        @staticmethod
        def now(tz=None):
            raise sentinel

    monkeypatch.setattr(auth_module, "datetime", BoomDateTime)

    with TestClient(app) as client, contextlib.suppress(RuntimeError):
        client.post("/login/verify", data={"token": raw}, follow_redirects=False)

    # Critical invariant: the consumption committed even though the handler
    # threw afterward. Replay must fail.
    with pg_session_factory() as s:
        row = s.query(MagicLinkToken).filter_by(token_hash=hash_token(raw)).one()
        assert row.consumed_at is not None, "token consumption was rolled back"


def test_login_rejects_oversized_email_without_consuming_limiter(
    monkeypatch, pg_url, pg_session_factory
):
    """An email >320 chars must not grow the SlidingWindowLimiter dict
    (memory-amplification DoS) and must not reach the DB.
    """
    app, sender = _mk_app(monkeypatch, pg_url)
    huge = "a" * 400 + "@ki.se"

    with TestClient(app) as client:
        r = client.post("/login", data={"email": huge})
    assert r.status_code == 200
    assert sender.sent == []
    # Limiter dict must be untouched by the oversized key.
    assert huge not in app.state.email_limiter._events


class _ExplodingSender:
    def send(self, *, to: str, subject: str, text_body: str) -> None:
        raise EmailSendFailed("simulated upstream outage")


def test_login_swallows_email_send_failure(monkeypatch, pg_url, pg_session_factory, caplog):
    """If the upstream email service fails, /login must still return the
    same 'check your email' page (no information leak via 500) and log
    the failure for operator visibility.
    """
    app, _ = _mk_app(monkeypatch, pg_url)
    app.state.email_sender = _ExplodingSender()

    with (
        caplog.at_level(logging.WARNING, logger="call_manager.web.routes.auth"),
        TestClient(app) as client,
    ):
        r = client.post("/login", data={"email": "alice@ki.se"})
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert any(getattr(rec, "event", None) == "login_email_send_failed" for rec in caplog.records)


def test_post_verify_is_rate_limited_by_ip(monkeypatch, pg_url, pg_session_factory):
    """The IP limiter moved to the verb that consumes — POST /login/verify."""
    app, _ = _mk_app(monkeypatch, pg_url)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=3, window_seconds=60)

    with TestClient(app) as client:
        for _ in range(3):
            r = client.post("/login/verify", data={"token": "nope"}, follow_redirects=False)
            assert r.status_code == 400
        r = client.post("/login/verify", data={"token": "nope"}, follow_redirects=False)
    assert r.status_code == 429


@pytest.mark.parametrize(
    "expected_event",
    ["login_requested", "login_email_sent"],
)
def test_login_emits_structured_logs(
    monkeypatch, pg_url, pg_session_factory, caplog, expected_event
):
    app, _ = _mk_app(monkeypatch, pg_url)
    with (
        caplog.at_level(logging.INFO, logger="call_manager.web.routes.auth"),
        TestClient(app) as client,
    ):
        client.post("/login", data={"email": "alice@ki.se"})
    assert any(getattr(rec, "event", None) == expected_event for rec in caplog.records)


def test_trusted_proxy_reads_x_forwarded_for(monkeypatch, pg_url, pg_session_factory):
    """With TRUSTED_PROXY=true, the first X-Forwarded-For entry keys the
    IP rate-limiter. Two clients with distinct XFF values must get
    independent buckets even though they share the same TestClient host.
    """
    monkeypatch.setenv("TRUSTED_PROXY", "true")
    app, _ = _mk_app(monkeypatch, pg_url)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=1, window_seconds=60)

    with TestClient(app) as client:
        # Client A: first request allowed, second blocked.
        client.post("/login", data={"email": "a@ki.se"}, headers={"X-Forwarded-For": "1.1.1.1"})
        # Client B (different XFF): must still be allowed.
        client.post("/login", data={"email": "b@ki.se"}, headers={"X-Forwarded-For": "2.2.2.2"})

    assert "1.1.1.1" in app.state.ip_limiter._events
    assert "2.2.2.2" in app.state.ip_limiter._events


def test_trusted_proxy_picks_rightmost_xff_to_resist_spoofing(
    monkeypatch, pg_url, pg_session_factory
):
    """With `$proxy_add_x_forwarded_for` (Nginx default), the trusted proxy
    APPENDS the connecting-peer IP at the end of XFF. A client that sends
    its own XFF header gets the spoofed value in the leftmost slot and the
    real peer IP in the rightmost slot. Rate-limit bucketing must key off
    the real peer IP (rightmost) or a spoofed XFF defeats rate limiting.
    """
    monkeypatch.setenv("TRUSTED_PROXY", "true")
    app, _ = _mk_app(monkeypatch, pg_url)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=1, window_seconds=60)

    with TestClient(app) as client:
        client.post(
            "/login",
            data={"email": "a@ki.se"},
            # Attacker spoofs "1.1.1.1" at the head; proxy appended the real
            # peer "9.9.9.9" at the tail. The bucket key must be 9.9.9.9.
            headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"},
        )

    assert "9.9.9.9" in app.state.ip_limiter._events
    assert "1.1.1.1" not in app.state.ip_limiter._events


def test_untrusted_proxy_ignores_x_forwarded_for(monkeypatch, pg_url, pg_session_factory):
    """Default is untrusted: XFF must be ignored so a caller can't spoof
    their rate-limit bucket. Both requests key to the TestClient IP."""
    app, _ = _mk_app(monkeypatch, pg_url)
    assert app.state.settings.trusted_proxy is False

    with TestClient(app) as client:
        client.post(
            "/login",
            data={"email": "a@ki.se"},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )

    assert "9.9.9.9" not in app.state.ip_limiter._events


def test_get_verify_renders_interstitial_without_consuming(
    monkeypatch, pg_url, pg_session_factory
):
    """GET /login/verify renders the confirm page and does NOT consume the
    token — a mail-scanner pre-fetch must leave it valid for the human."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.get(f"/login/verify?token={raw}", follow_redirects=False)
        assert r.status_code == 200
        assert "confirm sign-in" in r.text.lower()
        # Token must NOT leak to the same-origin /static assets the page loads.
        assert r.headers["referrer-policy"] == "strict-origin"
        # Token still valid: the human's subsequent POST succeeds.
        p = client.post("/login/verify", data={"token": raw}, follow_redirects=False)
    assert p.status_code == 303
    assert "session" in p.cookies


def test_get_verify_renders_with_unknown_token(monkeypatch, pg_url):
    """The interstitial renders even for a bogus token — no DB lookup on GET."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/login/verify?token=garbage-token", follow_redirects=False)
    assert r.status_code == 200
    assert "confirm sign-in" in r.text.lower()


def test_post_verify_consumes_and_redirects_to_next(
    monkeypatch, pg_url, pg_session_factory
):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.post(
            "/login/verify",
            data={"token": raw, "next": "/calls/5?profile=3"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/calls/5?profile=3"
    assert "session" in r.cookies


def test_post_verify_unsafe_next_falls_back_to_root(
    monkeypatch, pg_url, pg_session_factory
):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.post(
            "/login/verify",
            data={"token": raw, "next": "https://evil.com"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_post_verify_succeeds_under_origin_csrf_check(
    monkeypatch, pg_url, pg_session_factory
):
    """The real interstitial POST carries same-origin Origin/Referer; the
    CSRF middleware must allow it. Regression guard against reintroducing
    no-referrer / Origin: null on the verify response."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="alice@ki.se")
        s.add(u)
        s.flush()
        raw = issue_token(s, user_id=u.id, ttl_minutes=15)
        s.commit()

    with TestClient(app) as client:
        r = client.post(
            "/login/verify",
            data={"token": raw},
            headers={
                "Origin": "http://testserver",
                "Referer": "http://testserver/login/verify",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "session" in r.cookies
