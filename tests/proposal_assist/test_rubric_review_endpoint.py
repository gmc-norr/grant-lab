"""Owner-only rubric review endpoint. TestClient pattern mirrors
tests/test_web_admin_digest_panel.py."""

import uuid

from fastapi.testclient import TestClient

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User, Workspace
from call_monitor.main import create_app


def _build_app(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _authed_client(app, *, user_id: int) -> TestClient:
    """Authenticated TestClient. Caller must client.__exit__ on cleanup
    (we enter the context manager here to fire lifespan)."""
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _make_owner_and_workspace(session, *, owner_email: str = "owner@ki.se"):
    """Create a User + a Workspace owned by them with a non-empty rubric.
    Returns (user_id, workspace_id)."""
    owner = User(email=owner_email)
    session.add(owner)
    session.flush()
    ws = Workspace(
        owner_id=owner.id,
        title="Test workspace",
        rubric_criteria=[
            {"name": "Excellence", "description": "x", "weight": 1.0, "scale_max": 5},
        ],
    )
    session.add(ws)
    session.commit()
    return owner.id, ws.id


def test_rubric_review_get_returns_form_for_owner(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        uid, wsid = _make_owner_and_workspace(s)

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}/rubric-review")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "Excellence" in resp.text


def test_rubric_review_get_returns_403_for_non_owner(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        _, wsid = _make_owner_and_workspace(s, owner_email="owner@ki.se")
        other = User(email="other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id

    client = _authed_client(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}/rubric-review")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_rubric_review_get_returns_404_for_missing_workspace(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        u = User(email="x@ki.se")
        s.add(u)
        s.commit()
        uid = u.id

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{uuid.uuid4()}/rubric-review")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404
