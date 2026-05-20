import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import DigestRun, User
from call_manager.main import create_app


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", "admin@ki.se")
    return create_app()


def _admin_client(app, pg_session_factory):
    """Return an authenticated TestClient for the admin user.

    The admin bootstrap only runs inside FastAPI's lifespan, which TestClient
    triggers when used as a context manager.  We enter the client once to let
    lifespan fire (and bootstrap the admin@ki.se user), then rebuild the cookie
    and return the still-open client for the actual test request.
    """
    from sqlalchemy import select

    client = TestClient(app, raise_server_exceptions=True)
    # Enter lifespan so bootstrap_admins runs and creates admin@ki.se.
    client.__enter__()
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_admin_panel_shows_zero_when_no_runs(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    client = _admin_client(app, pg_session_factory)
    try:
        r = client.get("/admin")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200
    assert "Digest" in r.text
    assert "Last digest sent" in r.text or "No digests sent" in r.text


def test_admin_panel_shows_recent_run_summary(monkeypatch, pg_url, pg_session_factory):
    from sqlalchemy import select

    app = _setup(monkeypatch, pg_url)
    # Bootstrap must run first; spin up a temporary client to trigger lifespan,
    # then add the DigestRun before making the actual assertion request.
    with TestClient(app) as tmp:
        tmp.get("/health")  # ensure lifespan fires
    with pg_session_factory() as s:
        u = s.execute(select(User).where(User.email == "admin@ki.se")).scalar_one()
        s.add(DigestRun(user_id=u.id, sent_at=datetime.now(UTC), included_call_ids=[1, 2, 3]))
        s.commit()
    client = _admin_client(app, pg_session_factory)
    try:
        r = client.get("/admin")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200
    assert "Digest" in r.text
    # The <dd> following "Runs this week" should contain "1" since we inserted one run
    m = re.search(r"Runs this week</dt>\s*<dd>\s*(\d+)\s*</dd>", r.text)
    assert m is not None, "Runs-this-week row not found"
    assert int(m.group(1)) == 1


def test_admin_panel_shows_disabled_user_count(monkeypatch, pg_url, pg_session_factory):
    app = _setup(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="disabled@ki.se", digest_disabled_at=datetime.now(UTC))
        s.add(u)
        s.commit()
    client = _admin_client(app, pg_session_factory)
    try:
        r = client.get("/admin")
    finally:
        client.__exit__(None, None, None)
    m = re.search(r"Disabled users[^<]*</dt>\s*<dd>\s*(\d+)\s*</dd>", r.text)
    assert m is not None, "Disabled-users row not found"
    assert int(m.group(1)) == 1
