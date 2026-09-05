import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from grant_lab.db.models import ChatTurn, TurnJob
from grant_lab.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    TurnDoneEvent,
)
from grant_lab.proposal_assist.drafting.controller import WorkspaceController
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry
from grant_lab.proposal_assist.turns.drain import drain_turn_queue_tick
from grant_lab.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.mark.asyncio
async def test_drain_tick_processes_one_queued_turn_per_workspace(
    pg_session_factory,
    _settings_env,
):
    """With two workspaces each having one queued turn, the drain tick
    dispatches both (one per workspace)."""
    from grant_lab.config import Settings

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
                content_text="hi",
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


@pytest.mark.asyncio
async def test_drain_skips_workspace_with_running_turn(
    pg_session_factory,
    _settings_env,
):
    """When a workspace has both a running turn_job and a newer queued one,
    drain MUST skip the queued one. Otherwise the atomic claim in
    process_turn_once would trip the partial unique index on
    turn_jobs(workspace_id) WHERE status='running', raising
    IntegrityError once per drain tick."""
    from datetime import UTC, datetime

    adapter = FakeAgentAdapter()
    # No scripted turn — if drain accidentally dispatches, the adapter
    # raises which surfaces via the drain tick's exception logging.
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
        running_user_turn = ChatTurn(workspace_id=ws.id, role="user", content="r")
        queued_user_turn = ChatTurn(workspace_id=ws.id, role="user", content="q")
        s.add_all([running_user_turn, queued_user_turn])
        s.flush()
        running = TurnJob(
            workspace_id=ws.id,
            user_turn_id=running_user_turn.id,
            status="running",
            started_at=datetime.now(UTC),
            submitted_by_user_id=user.id,
        )
        queued = TurnJob(
            workspace_id=ws.id,
            user_turn_id=queued_user_turn.id,
            status="queued",
            submitted_by_user_id=user.id,
        )
        s.add_all([running, queued])
        s.commit()
        queued_id = queued.id

    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
    )
    assert dispatched == 0

    with pg_session_factory() as s:
        still_queued = s.get(TurnJob, queued_id)
        assert still_queued.status == "queued"


@pytest.mark.asyncio
async def test_drain_skips_soft_deleted_workspace(
    pg_session_factory,
    _settings_env,
):
    """A queued turn for a soft-deleted workspace must NOT be dispatched —
    a deleted workspace mustn't keep spending tokens or editing its repo."""
    from datetime import UTC, datetime

    adapter = FakeAgentAdapter()
    # No scripted turn: a wrongful dispatch would make the adapter raise.
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=user)
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()
        t = ChatTurn(workspace_id=ws.id, role="user", content="q")
        s.add(t)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=t.id,
            status="queued",
            submitted_by_user_id=user.id,
        )
        s.add(job)
        s.commit()
        job_id = job.id

    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
    )
    assert dispatched == 0
    assert adapter.launches == []
    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "queued"


class _SlowAdapter(FakeAgentAdapter):
    """send_turn blocks past any sane turn_max_seconds before yielding, so a
    drain tick with a small timeout cancels it mid-stream."""

    async def send_turn(self, handle, user_message) -> AsyncIterator[TurnDoneEvent]:  # noqa: ANN001
        self.last_user_message = user_message
        self.turns.append((handle, user_message))
        await asyncio.sleep(30)  # cancelled by wait_for long before this returns
        yield TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="s")


@pytest.mark.asyncio
async def test_drain_terminates_handle_on_turn_timeout(
    pg_session_factory,
    _settings_env,
):
    """Regression: a turn exceeding turn_max_seconds must TERMINATE the SDK
    handle, not just cancel the coroutine. Otherwise the cached subprocess is
    reused with a half-drained message stream and the next turn reads the
    abandoned turn's leftover messages (cross-turn transcript/token corruption).
    After the timeout the handle must be gone so the next turn cold-launches."""
    adapter = _SlowAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        uid = user.id
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id
        t = ChatTurn(workspace_id=wsid, role="user", content="hi")
        s.add(t)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=wsid,
                user_turn_id=t.id,
                status="queued",
                submitted_by_user_id=uid,
            )
        )
        s.commit()

    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        turn_max_seconds=0.05,  # forces the timeout path
    )
    # Timed out → not counted as dispatched.
    assert dispatched == 0
    # The fix: the handle was terminated (not left cached with a dirty stream).
    assert len(adapter.terminations) == 1
    assert wsid not in registry._entries


class _InterruptRecordingAdapter(_SlowAdapter):
    """Records interrupt() calls so the drain timeout path can be asserted to
    attempt a graceful interrupt before falling back to terminate."""

    def __init__(self):
        super().__init__()
        self.interrupts: list = []

    async def interrupt(self, handle) -> None:  # noqa: ANN001
        self.interrupts.append(handle.workspace_id)


@pytest.mark.asyncio
async def test_drain_attempts_interrupt_before_terminate_on_timeout(
    pg_session_factory,
    _settings_env,
):
    """Defect 5: a timed-out turn should first try the SDK's interrupt() so the
    subprocess can stop cleanly, then terminate() remains the guaranteed
    backstop (handle gone after the tick)."""
    adapter = _InterruptRecordingAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        user = make_user(s)
        s.commit()
        uid = user.id
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id
        t = ChatTurn(workspace_id=wsid, role="user", content="hi")
        s.add(t)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=wsid,
                user_turn_id=t.id,
                status="queued",
                submitted_by_user_id=uid,
            )
        )
        s.commit()

    dispatched = await drain_turn_queue_tick(
        session_factory=pg_session_factory,
        controller=controller,
        turn_max_seconds=0.05,
    )
    assert dispatched == 0
    # Interrupt was attempted for the workspace...
    assert adapter.interrupts == [wsid]
    # ...and terminate still ran as the guaranteed backstop.
    assert len(adapter.terminations) == 1
    assert wsid not in registry._entries
