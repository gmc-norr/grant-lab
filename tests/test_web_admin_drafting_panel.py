"""Owner-side admin panel for the proposal-assist drafting subsystem.

Mirrors the test pattern from test_web_admin_digest_panel.py:
- enter TestClient context (fires lifespan → bootstrap_admins)
- promote a test user to admin via direct DB write
- sign session cookie via SessionSigner
- assert page renders with expected fragments
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import (
    ChatTurn,
    User,
    Workspace,
    WorkspaceEvent,
)
from call_manager.main import create_app


def _build_app(monkeypatch, pg_url, *, drafting_enabled: bool = True):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "true" if drafting_enabled else "false")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "1000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "500")
    return create_app()


def _admin_client(app, *, admin_email: str = "admin@ki.se") -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    # Promote a user to admin AFTER lifespan runs (so bootstrap doesn't fight us).
    from sqlalchemy.orm import sessionmaker

    sf: sessionmaker = app.state.session_factory
    with sf() as s:
        u = s.scalar(select(User).where(User.email == admin_email))
        if u is None:
            u = User(email=admin_email, is_admin=True)
            s.add(u)
            s.commit()
            s.refresh(u)
        elif not u.is_admin:
            u.is_admin = True
            s.commit()
        uid = u.id
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    return client


def test_drafting_panel_returns_403_for_non_admin(monkeypatch, pg_url, pg_session_factory):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="user@ki.se", is_admin=False)
        s.add(u)
        s.commit()
        uid = u.id
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_drafting_panel_renders_for_admin(monkeypatch, pg_url, pg_session_factory):
    app = _build_app(monkeypatch, pg_url, drafting_enabled=True)
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    # Kill switch state visible.
    assert "Drafting enabled" in resp.text or "enabled: true" in resp.text.lower()
    # Global cap caption visible.
    assert "1000" in resp.text or "1,000" in resp.text  # input cap default we set


def test_drafting_panel_shows_kill_switch_off(monkeypatch, pg_url, pg_session_factory):
    app = _build_app(monkeypatch, pg_url, drafting_enabled=False)
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "disabled" in resp.text.lower()


def test_drafting_panel_shows_per_workspace_usage(monkeypatch, pg_url, pg_session_factory):
    app = _build_app(monkeypatch, pg_url)

    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="VisibleInPanel")
        s.add(ws)
        s.flush()
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=42,
                tokens_out=7,
            )
        )
        s.commit()

    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "VisibleInPanel" in resp.text
    assert "42" in resp.text
    assert "7" in resp.text


def test_drafting_panel_shows_recent_failed_events(monkeypatch, pg_url, pg_session_factory):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="WithFails")
        s.add(ws)
        s.flush()
        for i in range(3):
            s.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="cloud_turn_failed",
                    actor_id=owner.id,
                    payload={
                        "turn_job_id": "00000000-0000-0000-0000-00000000000" + str(i),
                        "error": f"distinctive_test_error_{i}",
                    },
                )
            )
        s.commit()
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    # Distinctive error strings should surface in the recent-failures section.
    assert "distinctive_test_error_0" in resp.text
    assert "distinctive_test_error_2" in resp.text


def test_panel_shows_exceeded_state_when_over_cap(
    monkeypatch, pg_url, pg_session_factory
):
    """Seed ChatTurn.tokens_in above the global cap; the panel must
    show 'EXCEEDED' (not 'OK') for the input-row status. Locks in
    the b8-residuals readability rewrite of the input-row Jinja."""
    app = _build_app(monkeypatch, pg_url, drafting_enabled=True)
    with pg_session_factory() as s:
        owner = User(email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="OverCapWs")
        s.add(ws)
        s.flush()
        # _build_app sets GLOBAL_DAILY_TOKEN_CAP_INPUT=1000; seed 1500.
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=1500,
            )
        )
        s.commit()
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "EXCEEDED" in resp.text


def test_payload_with_html_chars_is_safely_rendered(
    monkeypatch, pg_url, pg_session_factory
):
    """Locks in the Jinja `tojson` autoescape contract for the
    recent-events table. A cloud_turn_failed event whose payload
    contains a <script> substring (e.g., an SDK exception message
    that quoted user input) MUST NOT render as live HTML."""
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        owner = User(email="o@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="W")
        s.add(ws)
        s.flush()
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="cloud_turn_failed",
                actor_id=owner.id,
                payload={"error": "<script>alert(1)</script>"},
            )
        )
        s.commit()
    client = _admin_client(app)
    try:
        resp = client.get("/admin/drafting")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    # tojson encodes < as <, and Jinja's HTML autoescape would
    # render either form safely. Accept either.
    assert "\\u003cscript" in resp.text or "&lt;script" in resp.text
