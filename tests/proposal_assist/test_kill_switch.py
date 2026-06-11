"""DRAFTING_ENABLED=false: POST /turns refuses with 503; drain
tick logs + no-ops so queued turns wait for re-enable."""

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


def test_post_turn_returns_503_when_kill_switch_off(monkeypatch, pg_url, pg_session_factory):
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
    client.post(f"/workspaces/{wsid}/disclosure")  # clear disclosure gate
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


def test_post_turn_works_when_kill_switch_on(monkeypatch, pg_url, pg_session_factory):
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
    client.post(f"/workspaces/{wsid}/disclosure")  # clear disclosure gate
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
        ws = make_workspace(s, owner=user, ack_disclosure=True)
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


def test_phase0_and_compaction_skip_when_disabled(monkeypatch):
    """DRAFTING_ENABLED=false must also gate _phase0_tick and
    _compaction_tick (both spend Anthropic tokens). Without this gate
    an operator flipping the kill switch to 'stop the bleeding' sees
    the user queue pause while extractors + compaction keep billing."""
    from unittest.mock import MagicMock

    from call_manager.config import Settings
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller
    from call_manager.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    settings = Settings()

    # Record whether the work functions get called.
    phase0_calls: list = []
    compaction_calls: list = []
    monkeypatch.setattr(
        "call_manager.scheduler.run_phase0_tick",
        lambda **kw: phase0_calls.append(kw),
    )
    monkeypatch.setattr(
        "call_manager.proposal_assist.turns.compaction.run_compaction_tick",
        lambda **kw: compaction_calls.append(kw),
    )

    def session_factory():  # never invoked because both ticks skip
        return None

    workspace_controller = build_workspace_controller(
        settings=settings, session_factory=session_factory
    )
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=MagicMock(),
        ollama_url=settings.ollama_url,
        gemma_model=settings.gemma_model,
        ranking_tick_seconds=settings.ranking_tick_seconds,
        ranking_queue_batch=settings.ranking_queue_batch,
        embedding_pregate_threshold=settings.embedding_pregate_threshold,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )

    # Invoke the registered closures directly.
    phase0_job = sched.get_job("proposal_assist_phase0_tick")
    compaction_job = sched.get_job("compaction_tick")
    assert phase0_job is not None, "phase0 tick must be registered"
    assert compaction_job is not None, "compaction tick must be registered"

    phase0_job.func()
    compaction_job.func()

    assert phase0_calls == [], "_phase0_tick must skip when drafting_enabled=False"
    assert compaction_calls == [], "_compaction_tick must skip when drafting_enabled=False"
    sched.shutdown(wait=False)


@pytest.mark.asyncio
async def test_re_enable_drains_queued_backlog(pg_session_factory, _settings_env):
    """Submit while disabled (simulated by inserting a queued TurnJob
    directly), then run drain twice: first with disabled=False (no-op),
    then with disabled=True (dispatches). Locks in re-enable resumption."""
    from datetime import UTC, datetime

    from call_manager.proposal_assist.drafting.contract import (
        AssistantTextEvent,
        TurnDoneEvent,
    )
    from call_manager.proposal_assist.drafting.controller import WorkspaceController
    from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
    from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
    from call_manager.proposal_assist.turns.drain import drain_turn_queue_tick
    from tests.proposal_assist.conftest import make_user, make_workspace

    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="s"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(registry=registry, session_factory=pg_session_factory)

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user, ack_disclosure=True)
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

    # First tick: kill switch OFF → no-op.
    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        drafting_enabled=False,
    )
    assert dispatched == 0
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
    assert job.status == "queued", "queued turns must wait for re-enable"

    # Second tick: kill switch ON → dispatches.
    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        drafting_enabled=True,
    )
    assert dispatched == 1
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
    assert job.status == "completed"
