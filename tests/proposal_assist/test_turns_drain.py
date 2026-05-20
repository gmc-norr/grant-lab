import pytest
from sqlalchemy import select

from call_manager.db.models import ChatTurn, TurnJob
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from call_manager.proposal_assist.turns.drain import drain_turn_queue_tick
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.mark.asyncio
async def test_drain_tick_processes_one_queued_turn_per_workspace(
    pg_session_factory,
    _settings_env,
):
    """With two workspaces each having one queued turn, the drain tick
    dispatches both (one per workspace)."""
    from call_manager.config import Settings

    settings = Settings()

    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="hello A"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="sA"),
        ]
    )
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="hello B"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="sB"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    job_ids = []
    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        uid = user.id
        for _ in range(2):
            ws = make_workspace(s, owner=user)
            s.commit()
            job = submit_user_turn(
                session=s,
                workspace_id=ws.id,
                content={"type": "text", "text": "hi"},
                submitted_by_user_id=uid,
                settings=settings,
            )
            s.commit()
            job_ids.append(job.id)

    await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
    )

    with pg_session_factory() as s:
        statuses = [s.scalar(select(TurnJob.status).where(TurnJob.id == j)) for j in job_ids]
    assert statuses == ["completed", "completed"]


@pytest.mark.asyncio
async def test_drain_tick_no_op_when_no_queued_turns(
    pg_session_factory,
    _settings_env,
):
    """Empty queue is a noop — no errors, no work."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
    )
    assert adapter.launches == []


@pytest.mark.asyncio
async def test_drain_tick_picks_exactly_one_when_queued_at_ties(
    pg_session_factory,
    _settings_env,
):
    """Regression for M2: two queued jobs for the same workspace with
    identical queued_at must produce exactly one dispatch per tick (the
    DISTINCT ON + id tie-break guarantees this)."""
    from datetime import UTC, datetime

    adapter = FakeAgentAdapter()
    adapter.script_next_turn(
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="s1"),
        ]
    )
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    same_ts = datetime.now(UTC)
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id
    with pg_session_factory() as s:
        # Two user turns + two queued jobs sharing the exact same queued_at.
        for _ in range(2):
            t = ChatTurn(workspace_id=wsid, role="user", content={"type": "text", "text": "hi"})
            s.add(t)
            s.flush()
            s.add(
                TurnJob(
                    workspace_id=wsid,
                    user_turn_id=t.id,
                    submitted_by_user_id=uid,
                    status="queued",
                    queued_at=same_ts,
                )
            )
        s.commit()

    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
    )
    assert dispatched == 1
    # The other one stays queued for the next tick.
    with pg_session_factory() as s:
        statuses = sorted(
            [r for r in s.scalars(select(TurnJob.status).where(TurnJob.workspace_id == wsid))]
        )
    assert statuses == ["completed", "queued"]
