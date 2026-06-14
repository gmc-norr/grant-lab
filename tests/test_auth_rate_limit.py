from fastapi.testclient import TestClient

from call_manager.auth.email import InMemoryEmailSender
from call_manager.auth.rate_limit import SlidingWindowLimiter
from call_manager.db.models import User
from call_manager.main import create_app


def test_allows_under_limit():
    rl = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        assert rl.allow("alice@ki.se") is True


def test_blocks_over_limit():
    rl = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    for _ in range(3):
        rl.allow("alice@ki.se")
    assert rl.allow("alice@ki.se") is False


def test_separate_keys_are_independent():
    rl = SlidingWindowLimiter(max_requests=1, window_seconds=60)
    assert rl.allow("a@ki.se") is True
    assert rl.allow("b@ki.se") is True
    assert rl.allow("a@ki.se") is False


def test_window_expires(monkeypatch):
    clock = {"t": 1000.0}

    def fake_time():
        return clock["t"]

    rl = SlidingWindowLimiter(max_requests=2, window_seconds=10, clock=fake_time)
    assert rl.allow("x") is True
    assert rl.allow("x") is True
    assert rl.allow("x") is False

    clock["t"] += 11  # past the window
    assert rl.allow("x") is True


def test_sweep_reclaims_expired_keys():
    """Long-running process must not retain keys whose window fully expired.
    Without this, the dict grows unboundedly over the process lifetime."""
    clock = {"t": 1000.0}

    def fake_time():
        return clock["t"]

    rl = SlidingWindowLimiter(max_requests=1, window_seconds=10, clock=fake_time)
    rl.allow("alice")
    rl.allow("bob")
    assert len(rl._events) == 2

    clock["t"] += 100  # every event is expired
    removed = rl.sweep()
    assert removed == 2
    assert rl._events == {}


def test_limiters_have_distinguishable_names():
    """Name is available so a future log/metric pass can tell counters apart."""
    ip = SlidingWindowLimiter(max_requests=1, window_seconds=10, name="ip")
    email = SlidingWindowLimiter(max_requests=1, window_seconds=10, name="email")
    assert ip.name == "ip"
    assert email.name == "email"


def _mk_login_app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    app.state.email_sender = InMemoryEmailSender()
    app.state.email_limiter = SlidingWindowLimiter(max_requests=5, window_seconds=60, name="email")
    app.state.ip_limiter = SlidingWindowLimiter(max_requests=1000, window_seconds=60, name="ip")
    return app


def test_disallowed_domain_does_not_consume_email_limiter(monkeypatch, pg_url, pg_session_factory):
    """An email that doesn't resolve to a mail-eligible user (disallowed
    domain) must NOT spend a per-email rate-limit token — otherwise an
    attacker could lock out a known address with junk submissions. The
    response must be byte-identical to the allowed-domain case so the
    endpoint stays enumeration-resistant.
    """
    app = _mk_login_app(monkeypatch, pg_url)
    with TestClient(app) as client:
        bogus = client.post("/login", data={"email": "intruder@example.com"})
        real = client.post("/login", data={"email": "alice@ki.se"})

    # Uniform-response contract: same status and body regardless of whether
    # the email resolves to a real, allowed user.
    assert bogus.status_code == real.status_code == 200
    assert bogus.text == real.text

    # The disallowed-domain attempt left no per-email bucket behind.
    assert "intruder@example.com" not in app.state.email_limiter._events
    # The allowed-domain attempt DID consume its bucket.
    assert "alice@ki.se" in app.state.email_limiter._events


def test_known_email_not_locked_out_by_disallowed_spam(monkeypatch, pg_url, pg_session_factory):
    """Spamming disallowed-domain logins must not exhaust the limiter such
    that a legitimate allowed email is blocked. (Pre-fix the email limiter
    ran before the domain check, but it keyed on the *normalized email*, so
    this specifically guards that no allowed bucket is collaterally spent.)
    """
    app = _mk_login_app(monkeypatch, pg_url)
    app.state.email_limiter = SlidingWindowLimiter(max_requests=2, window_seconds=60, name="email")
    # Magic-link is login-only: a link is only sent for an existing ACTIVE
    # account. Seed the victim so the allowance assertion is meaningful.
    with pg_session_factory() as s:
        s.add(User(status="active", email="victim@ki.se"))
        s.commit()
    with TestClient(app) as client:
        # Hammer disallowed-domain addresses.
        for i in range(10):
            client.post("/login", data={"email": f"junk{i}@evil.example"})
        # A real allowed email still gets its full allowance.
        r1 = client.post("/login", data={"email": "victim@ki.se"})
        r2 = client.post("/login", data={"email": "victim@ki.se"})
    assert r1.status_code == r2.status_code == 200
    sender = app.state.email_sender
    # Both allowed-email attempts were under the per-email cap → 2 emails.
    assert len(sender.sent) == 2
