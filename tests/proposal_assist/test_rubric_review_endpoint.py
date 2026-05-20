"""Owner-only rubric review endpoint. TestClient pattern mirrors
tests/test_web_admin_digest_panel.py."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from call_monitor.auth.session_cookie import SessionSigner
from call_monitor.db.models import User, Workspace, WorkspaceEvent, WorkspaceSetupJob
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


def test_rubric_review_post_writes_edited_event_and_advances_phase(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        uid, wsid = _make_owner_and_workspace(s)
        ws = s.get(Workspace, wsid)
        ws.rubric_criteria = [
            {"name": "Old", "description": "old", "weight": 1.0, "scale_max": 5},
        ]
        ws.rubric_funder_notes = ""
        ws.current_phase = "setup"
        s.add(
            WorkspaceSetupJob(
                workspace_id=wsid,
                status="waiting_for_rubric_review",
            )
        )
        s.commit()

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/rubric-review",
            data={
                "criteria[0][name]": "New",
                "criteria[0][description]": "new",
                "criteria[0][weight]": "1.0",
                "criteria[0][scale_max]": "5",
                "funder_notes": "updated",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/workspaces/{wsid}"

        # Follow the redirect and assert the target renders.
        target_resp = client.get(resp.headers["location"])
        assert target_resp.status_code == 200
        assert "Test workspace" in target_resp.text
    finally:
        client.__exit__(None, None, None)

    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        assert ws.rubric_criteria[0]["name"] == "New"
        assert ws.rubric_funder_notes == "updated"
        assert ws.current_phase == "spec"
        job = s.execute(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == wsid)
        ).scalar_one()
        assert job.status == "done"
        assert job.completed_at is not None
        edits = (
            s.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "rubric_edited",
                )
            )
            .scalars()
            .all()
        )
        assert len(edits) == 1
        assert edits[0].payload["old_criteria"][0]["name"] == "Old"
        assert edits[0].payload["new_criteria"][0]["name"] == "New"
        assert edits[0].payload["edited_by_user_id"] == uid


def test_rubric_review_post_confirm_without_edits_still_advances_phase(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        uid, wsid = _make_owner_and_workspace(s)
        ws = s.get(Workspace, wsid)
        ws.rubric_criteria = [
            {"name": "X", "description": "x", "weight": 1.0, "scale_max": 5},
        ]
        ws.current_phase = "setup"
        s.add(
            WorkspaceSetupJob(
                workspace_id=wsid,
                status="waiting_for_rubric_review",
            )
        )
        s.commit()

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/rubric-review",
            data={
                "criteria[0][name]": "X",
                "criteria[0][description]": "x",
                "criteria[0][weight]": "1.0",
                "criteria[0][scale_max]": "5",
                "funder_notes": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303  # phase advanced even without edits

        # Follow the redirect and assert the target renders.
        target_resp = client.get(resp.headers["location"])
        assert target_resp.status_code == 200
        assert "Test workspace" in target_resp.text
    finally:
        client.__exit__(None, None, None)

    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        job = s.execute(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == wsid)
        ).scalar_one()
        assert ws.current_phase == "spec"
        assert job.status == "done"
        # No rubric_edited event because content didn't change.
        edits = (
            s.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "rubric_edited",
                )
            )
            .scalars()
            .all()
        )
        assert len(edits) == 0


def test_rubric_review_post_returns_403_for_non_owner(
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
        resp = client.post(
            f"/workspaces/{wsid}/rubric-review",
            data={
                "criteria[0][name]": "X",
                "criteria[0][description]": "x",
                "criteria[0][weight]": "1.0",
                "criteria[0][scale_max]": "5",
                "funder_notes": "",
            },
            follow_redirects=False,
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_workspace_placeholder_returns_200_for_owner(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        uid, wsid = _make_owner_and_workspace(s)

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.get(f"/workspaces/{wsid}")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    assert "Test workspace" in resp.text
    assert "setup" in resp.text  # current_phase rendered


def test_workspace_placeholder_returns_403_for_non_owner(
    monkeypatch,
    pg_url,
    pg_session_factory,
):
    app = _build_app(monkeypatch, pg_url)
    with pg_session_factory() as s:
        _, wsid = _make_owner_and_workspace(s)
        other = User(email="other-ws@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id

    client = _authed_client(app, user_id=other_id)
    try:
        resp = client.get(f"/workspaces/{wsid}")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403
