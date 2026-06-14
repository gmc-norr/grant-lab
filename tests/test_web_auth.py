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


def _seed_active(pg_session_factory, email="alice@ki.se"):
    """Magic-link is login-only: a link is issued only for an existing ACTIVE
    account. Tests that assert an email is sent must seed one first."""
    with pg_session_factory() as s:
        s.add(User(status="active", email=email))
        s.commit()


def test_login_form_renders(monkeypatch, pg_url):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 200
    assert "email" in r.text.lower()


def test_login_post_sends_email_for_allowed_domain(monkeypatch, pg_url, pg_session_factory):
    app, sender = _mk_app(monkeypatch, pg_url)
    _seed_active(pg_session_factory)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "alice@ki.se"}, follow_redirects=False)
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert len(sender.sent) == 1
    assert sender.sent[0].to == "alice@ki.se"
    assert "/login/verify?token=" in sender.sent[0].text_body


def test_login_post_does_not_create_user_on_first_request(monkeypatch, pg_url, pg_session_factory):
    """Magic-link is login-only: posting an unknown email must NOT auto-create
    a user (provisioning happens only via the admin-gated Google flow)."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "brand-new@ki.se"})
    assert r.status_code == 200
    with pg_session_factory() as s:
        assert s.query(User).filter_by(email="brand-new@ki.se").one_or_none() is None


def test_login_post_silently_drops_disallowed_domain(monkeypatch, pg_url, pg_session_factory):
    app, sender = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "intruder@example.com"})
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert sender.sent == []


def test_login_post_is_rate_limited_per_email(monkeypatch, pg_url, pg_session_factory):
    app, sender = _mk_app(monkeypatch, pg_url)
    _seed_active(pg_session_factory)
    app.state.email_limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60)
    with TestClient(app) as client:
        for _ in range(5):
            r = client.post("/login", data={"email": "alice@ki.se"})
            assert r.status_code == 200
    assert len(sender.sent) == 2


def test_verify_sets_session_cookie_and_redirects(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se")
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
        u = User(status="active", email="alice@ki.se")
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
    assert "text/html" in r.headers["content-type"]


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
        u = User(status="active", email="alice@ki.se")
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
    _seed_active(pg_session_factory)

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
    _seed_active(pg_session_factory)
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


def test_get_verify_renders_interstitial_without_consuming(monkeypatch, pg_url, pg_session_factory):
    """GET /login/verify renders the confirm page and does NOT consume the
    token — a mail-scanner pre-fetch must leave it valid for the human."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se")
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


def test_post_verify_consumes_and_redirects_to_next(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se")
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


def test_post_verify_unsafe_next_falls_back_to_root(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se")
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


def test_post_verify_succeeds_under_origin_csrf_check(monkeypatch, pg_url, pg_session_factory):
    """The real interstitial POST carries same-origin Origin/Referer; the
    CSRF middleware must allow it. Regression guard against reintroducing
    no-referrer / Origin: null on the verify response."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se")
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


def test_html_401_get_threads_next(monkeypatch, pg_url):
    """An unauthenticated browser GET on a protected path redirects to
    /login carrying the urlencoded relative path (path + query)."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get(
            "/calls/5?profile=3",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=%2Fcalls%2F5%3Fprofile%3D3"


def test_html_401_post_does_not_thread_next(monkeypatch, pg_url):
    """A POST that 401s must redirect to bare /login (no next) — threading
    a POST path would 303-GET into a POST-only route and 405."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(
            "/calls/5/hide",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_json_401_unaffected_by_next(monkeypatch, pg_url):
    """A non-HTML caller still gets the raw JSON 401, no redirect."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.get("/calls/5?profile=3", follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")


def test_next_threaded_end_to_end_with_nested_query(monkeypatch, pg_url, pg_session_factory):
    """Full digest journey, exercising BOTH browser handoffs that carry
    next: GET /login?next renders the hidden field; the user submits the
    value FROM that form; the emailed magic-link GET renders the hidden
    token+next on the interstitial; the user POSTs the values FROM that
    form; the final redirect lands on the deep link with both query params
    intact. Reading from the rendered forms (not hand-passing next) means a
    dropped hidden field FAILS the test rather than passing silently."""
    import html
    import re

    app, sender = _mk_app(monkeypatch, pg_url)
    _seed_active(pg_session_factory)
    nxt = "/calls/5?profile=3&source=x"

    def hidden(text, name):
        # Pull a hidden input's value, tolerating HTML-escaping (& -> &amp;).
        m = re.search(rf'name="{name}"[^>]*value="([^"]*)"', text)
        return html.unescape(m.group(1)) if m else None

    with TestClient(app) as client:
        # 1. Browser hits /login?next — the hidden next field must render.
        g = client.get("/login", params={"next": nxt})
        assert g.status_code == 200
        assert hidden(g.text, "next") == nxt

        # 2. Submit the email WITH the next value read from the rendered form.
        client.post("/login", data={"email": "alice@ki.se", "next": hidden(g.text, "next")})
        verify_url = re.search(r"(/login/verify\?\S+)", sender.sent[-1].text_body).group(1)

        # 3. Browser GETs the emailed link → interstitial renders hidden
        #    token + next (no consume).
        gi = client.get(verify_url, follow_redirects=False)
        assert gi.status_code == 200
        token = hidden(gi.text, "token")
        next_field = hidden(gi.text, "next")
        assert token is not None
        assert next_field == nxt  # encode→email→decode→render round-trip intact

        # 4. Confirm — POST the values extracted FROM the rendered form.
        r = client.post(
            "/login/verify",
            data={"token": token, "next": next_field},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/calls/5?profile=3&source=x"


def test_login_sent_preserves_next_in_back_link(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "alice@ki.se", "next": "/calls/5?profile=3"})
    assert r.status_code == 200
    assert 'href="/login?next=%2Fcalls%2F5%3Fprofile%3D3"' in r.text


def test_login_post_drops_unsafe_next(monkeypatch, pg_url, pg_session_factory):
    """An open-redirect next must never reach the magic-link URL."""
    app, sender = _mk_app(monkeypatch, pg_url)
    _seed_active(pg_session_factory)
    with TestClient(app) as client:
        client.post("/login", data={"email": "alice@ki.se", "next": "https://evil.com"})
    assert len(sender.sent) == 1
    body = sender.sent[-1].text_body
    assert "evil.com" not in body
    assert "next=" not in body


def test_login_enumeration_resistant_with_next(monkeypatch, pg_url, pg_session_factory):
    """next must not change the response across allow/deny/rate-limited branches."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        allowed = client.post("/login", data={"email": "alice@ki.se", "next": "/calls/5"})
        denied = client.post("/login", data={"email": "x@evil.com", "next": "/calls/5"})
        # The rate-limited branch must also return the identical page.
        app.state.ip_limiter = SlidingWindowLimiter(max_requests=1, window_seconds=60)
        client.post("/login", data={"email": "alice@ki.se", "next": "/calls/5"})
        rate_limited = client.post("/login", data={"email": "alice@ki.se", "next": "/calls/5"})
    assert allowed.status_code == denied.status_code == rate_limited.status_code == 200
    assert allowed.text == denied.text == rate_limited.text  # byte-identical


def test_login_form_shows_configured_domain(monkeypatch, pg_url):
    """The login page derives its domain hint from config — not a hardcoded
    value. Configure a NON-default domain and assert the old hardcoded
    `you@ki.se` is gone (so the test can't pass before implementation)."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "example.org")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 200
    assert "you@example.org" in r.text  # placeholder derived from config
    assert "you@ki.se" not in r.text  # the old hardcoded value is gone


def test_expired_link_post_renders_recovery_page(monkeypatch, pg_url, pg_session_factory):
    """A human clicking a stale interstitial (bad/used token) gets the
    dedicated recovery page at 400, with an inline new-link form that
    forwards the original next."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post(
            "/login/verify",
            data={"token": "already-used-or-bogus", "next": "/calls/5?profile=3"},
            follow_redirects=False,
        )
    assert r.status_code == 400
    text = r.text.lower()
    assert "expired" in text or "already used" in text
    assert 'action="/login"' in r.text  # inline new-link form
    assert 'value="/calls/5?profile=3"' in r.text  # next forwarded


def test_login_sent_shows_spam_guidance(monkeypatch, pg_url, pg_session_factory):
    app, _ = _mk_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        r = client.post("/login", data={"email": "alice@ki.se"})
    assert r.status_code == 200
    assert "spam" in r.text.lower()


def test_replay_renders_recovery_page(monkeypatch, pg_url, pg_session_factory):
    """The single-use replay (Task 2 asserted status 400) now lands on the
    recovery page — still 400, but the dedicated body."""
    app, _ = _mk_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(status="active", email="alice@ki.se")
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
    assert "expired" in r.text.lower() or "already used" in r.text.lower()
    assert 'value="alice@' not in r.text
