"""DRAFTING_ENABLED=false: POST /turns refuses with 503; drain
tick logs + no-ops so queued turns wait for re-enable."""

import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import ChatTurn, TurnJob, User, Workspace
from call_manager.main import create_app


def _build_app_kill(monkeypatch, pg_url, *, drafting_enabled: bool):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "true" if drafting_enabled else "false")
    return create_app()


def _authed_client(app, *, user_id: int) -> TestClient:
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def test_post_turn_returns_503_when_kill_switch_off(
    monkeypatch, pg_url, pg_session_factory
):
    app = _build_app_kill(monkeypatch, pg_url, drafting_enabled=False)
    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="Test")
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 503
    assert "drafting" in resp.text.lower()
    # No ChatTurn or TurnJob persisted.
    with pg_session_factory() as s:
        turns = list(s.scalars(select(ChatTurn).where(ChatTurn.workspace_id == wsid)))
        jobs = list(s.scalars(select(TurnJob).where(TurnJob.workspace_id == wsid)))
    assert turns == []
    assert jobs == []


def test_post_turn_works_when_kill_switch_on(
    monkeypatch, pg_url, pg_session_factory
):
    """Sanity: defaulted/explicitly-True kill switch lets submits through."""
    app = _build_app_kill(monkeypatch, pg_url, drafting_enabled=True)
    with pg_session_factory() as s:
        owner = User(email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="Test")
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = _authed_client(app, user_id=uid)
    try:
        resp = client.post(f"/workspaces/{wsid}/turns", data={"content": "hi"})
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    with pg_session_factory() as s:
        jobs = list(s.scalars(select(TurnJob).where(TurnJob.workspace_id == wsid)))
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_drain_tick_noops_when_kill_switch_off(
    pg_session_factory,
    _settings_env,
):
    """When DRAFTING_ENABLED=False, drain_turn_queue_tick must NOT
    dispatch even if a queued turn exists. The turn stays queued so
    re-enabling resumes work."""
    from datetime import UTC, datetime

    from call_manager.proposal_assist.drafting.controller import WorkspaceController
    from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
    from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
    from call_manager.proposal_assist.turns.drain import drain_turn_queue_tick
    from tests.proposal_assist.conftest import make_user, make_workspace

    adapter = FakeAgentAdapter()
    # No scripted turn: if drain dispatches by mistake, the adapter
    # raises and the test sees a non-zero dispatched count.
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        user_turn = ChatTurn(workspace_id=ws.id, role="user", content="hi")
        s.add(user_turn)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=user_turn.id,
                status="queued",
                queued_at=datetime.now(UTC),
                submitted_by_user_id=user.id,
            )
        )
        s.commit()
        wsid = ws.id

    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        drafting_enabled=False,
    )
    assert dispatched == 0
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
    assert job.status == "queued", "queued turns must wait for re-enable"
