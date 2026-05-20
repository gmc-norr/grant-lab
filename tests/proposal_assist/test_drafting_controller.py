import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_monitor.db.models import TurnJob
from call_monitor.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.locking import acquire_workspace_lock
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry
from tests.proposal_assist.conftest import make_chat_turn, make_workspace


@pytest.mark.asyncio
async def test_controller_launch_returns_handle(pg_session_factory):
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    handle = await controller.launch(wsid)
    assert handle.workspace_id == wsid
    assert adapter.launches[0].workspace_id == wsid


@pytest.mark.asyncio
async def test_controller_launch_runs_orphan_sweep(pg_session_factory):
    """A stale running turn_jobs row is transitioned to failed before
    the new handle is handed back."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            TurnJob(
                workspace_id=ws.id,
                status="running",
                started_at=datetime.now(UTC),
            )
        )
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
        assert job.error == "orphaned"


@pytest.mark.asyncio
async def test_controller_launch_passes_resume_messages_to_adapter(
    pg_session_factory,
):
    """The controller builds a LaunchContext with resume messages from
    history and passes it to the adapter at launch time. For the fake
    adapter, we verify they were recorded on the handle's state."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": "old turn"})
        s.commit()
        wsid = ws.id

    handle = await controller.launch(wsid)

    resume = handle.state.get("resume_messages")
    assert resume is not None
    assert len(resume) == 1
    assert resume[0]["content"] == {"type": "text", "text": "old turn"}


@pytest.mark.asyncio
async def test_controller_launch_holds_advisory_lock_for_handle_lifetime(
    pg_session_factory,
):
    """The advisory lock is acquired at launch and held until terminate.
    While a handle is live, a second session cannot acquire the lock.
    After terminate, the lock is released."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)

    # Lock should be held — a second session cannot acquire it.
    with pg_session_factory() as s2:
        assert acquire_workspace_lock(s2, wsid) is False

    # After terminate, the lock is released.
    await controller.terminate(wsid)
    with pg_session_factory() as s3:
        assert acquire_workspace_lock(s3, wsid) is True


@pytest.mark.asyncio
async def test_controller_launch_returns_cached_handle_on_second_call(
    pg_session_factory,
):
    """Second launch of a live workspace returns the cached handle."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    h1 = await controller.launch(wsid)
    h2 = await controller.launch(wsid)
    assert h1 is h2


@pytest.mark.asyncio
async def test_controller_send_turn_yields_events(pg_session_factory):
    """Controller.send_turn iterates the adapter's stream and touches
    the registry's idle timer along the way."""
    from call_monitor.proposal_assist.drafting.contract import (
        AssistantTextEvent,
        TurnDoneEvent,
    )

    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="reply"),
            TurnDoneEvent(tokens_in=5, tokens_out=10, model="fake"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    events = [event async for event in controller.send_turn(wsid, "hi")]
    assert len(events) == 2
    assert isinstance(events[0], AssistantTextEvent)
    assert isinstance(events[1], TurnDoneEvent)
