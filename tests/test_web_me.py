from fastapi.testclient import TestClient

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User
from call_monitor.main import create_app


def _logged_in_client(monkeypatch, pg_url, pg_session_factory, *, email="alice@ki.se"):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()

    with pg_session_factory() as s:
        u = User(email=email)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id

    signer: SessionSigner = app.state.session_signer
    client = TestClient(app)
    client.cookies.set("session", signer.make(user_id=uid))
    return client, uid


def test_me_requires_auth(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/me")
    assert r.status_code == 401


def test_me_renders_current_user(monkeypatch, pg_url, pg_session_factory):
    client, _ = _logged_in_client(monkeypatch, pg_url, pg_session_factory)
    with client:
        r = client.get("/me")
    assert r.status_code == 200
    assert "alice@ki.se" in r.text


def test_me_post_updates_name(monkeypatch, pg_url, pg_session_factory):
    client, uid = _logged_in_client(monkeypatch, pg_url, pg_session_factory)
    with client:
        r = client.post("/me", data={"name": "Alice Andersson"})
    assert r.status_code == 200
    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.name == "Alice Andersson"


def test_me_escapes_html_in_display_name(monkeypatch, pg_url, pg_session_factory):
    """Jinja2 autoescape regression guard. A display name containing
    raw HTML must render escaped — if a future change turns off
    autoescape or adds a `|safe` filter, this test catches it before
    it becomes a stored-XSS vulnerability."""
    client, uid = _logged_in_client(monkeypatch, pg_url, pg_session_factory)
    with pg_session_factory() as s:
        u = s.get(User, uid)
        u.name = "<script>alert('xss')</script>"
        s.commit()

    with client:
        r = client.get("/me")
    assert r.status_code == 200
    # Raw <script> must NOT be present in the response body.
    assert "<script>alert" not in r.text
    # Escaped form must be present.
    assert "&lt;script&gt;alert" in r.text


def test_static_htmx_is_served_locally(monkeypatch, pg_url):
    """htmx must be served from the app itself, not pulled from a CDN on
    every logged-in page load. Regression guard: if someone re-points to
    unpkg, this fails."""
    from fastapi.testclient import TestClient

    from call_monitor.main import create_app
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/static/htmx.min.js")
        login = client.get("/login")
    assert r.status_code == 200
    assert b"htmx" in r.content.lower() or len(r.content) > 1000
    assert "/static/htmx.min.js" in login.text
    assert "unpkg.com" not in login.text


def test_me_post_returns_401_when_user_gone(monkeypatch, pg_url, pg_session_factory):
    """If the user is deleted between get_current_user resolving the cookie
    and the POST handler re-fetching by id, we must 401 — not 500 on
    `None.name = ...`.
    """
    from call_monitor.auth import dependencies as deps

    client, uid = _logged_in_client(monkeypatch, pg_url, pg_session_factory)

    # Simulate the race: return a stale User object for the cookie check,
    # then the POST handler's fresh session.get() will find nothing.
    stale = User(id=uid, email="gone@ki.se", is_admin=False)

    def fake_get_current_user():
        return stale

    # Overriding via FastAPI's dependency_overrides is cleaner than monkeypatching.
    client.app.dependency_overrides[deps.get_current_user] = fake_get_current_user

    with pg_session_factory() as s:
        s.query(User).filter_by(id=uid).delete()
        s.commit()

    with client:
        r = client.post("/me", data={"name": "Whatever"})
    assert r.status_code == 401
