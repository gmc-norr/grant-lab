from fastapi.testclient import TestClient

from call_manager.auth.email import InMemoryEmailSender
from call_manager.auth.rate_limit import SlidingWindowLimiter
from call_manager.main import create_app

# A 12+ char password so the form's required `password` field is populated and
# the login handler actually runs (these tests exercise IP/limiter plumbing and
# form rendering, not credential success — every login here legitimately fails).
_PW = "irrelevant-but-long"


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
    assert "password" in r.text.lower()


def test_login_rejects_oversized_email_without_consuming_limiter(
    monkeypatch, pg_url, pg_session_factory
):
    """An email >320 chars must not grow the SlidingWindowLimiter dict
    (memory-amplification DoS) and must not reach the DB.
    """
    app, _ = _mk_app(monkeypatch, pg_url)
    huge = "a" * 400 + "@ki.se"

    with TestClient(app) as client:
        r = client.post("/login", data={"email": huge, "password": _PW})
    assert r.status_code == 200
    # Limiter dict must be untouched by the oversized key.
    assert huge not in app.state.email_limiter._events


def test_trusted_proxy_reads_x_forwarded_for(monkeypatch, pg_url, pg_session_factory):
    """With TRUSTED_PROXY=true, the X-Forwarded-For entry keys the IP
    rate-limiter. Two clients with distinct XFF values must get independent
    buckets even though they share the same TestClient host.
    """
    monkeypatch.setenv("TRUSTED_PROXY", "true")
    app, _ = _mk_app(monkeypatch, pg_url)
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=1, window_seconds=60)

    with TestClient(app) as client:
        client.post(
            "/login",
            data={"email": "a@ki.se", "password": _PW},
            headers={"X-Forwarded-For": "1.1.1.1"},
        )
        client.post(
            "/login",
            data={"email": "b@ki.se", "password": _PW},
            headers={"X-Forwarded-For": "2.2.2.2"},
        )

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
            data={"email": "a@ki.se", "password": _PW},
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
            data={"email": "a@ki.se", "password": _PW},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )

    assert "9.9.9.9" not in app.state.ip_limiter._events


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
