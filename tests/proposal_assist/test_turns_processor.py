import pytest
from sqlalchemy import select

from call_manager.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    SDKTurnError,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from call_manager.proposal_assist.turns.processor import process_turn_once
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


def _build_controller_and_submit(pg_session_factory, _settings_env, events):
    from call_manager.config import Settings

    settings = Settings()
    adapter = FakeAgentAdapter()
    adapter.script_next_turn(events)
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="Hi",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        job_id = job.id
    return wsid, job_id, controller, adapter


@pytest.mark.asyncio
async def test_process_turn_writes_assistant_text_event(
    pg_session_factory,
    _settings_env,
):
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [
            AssistantTextEvent(text="Hello back."),
            TurnDoneEvent(tokens_in=10, tokens_out=20, model="fake", session_id="s1"),
        ],
    )

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        turns = list(
            s.scalars(
                select(ChatTurn).where(ChatTurn.workspace_id == wsid).order_by(ChatTurn.position)
            )
        )
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        ws = s.scalar(select(Workspace).where(Workspace.id == wsid))

    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[1].content == {"type": "text", "text": "Hello back."}
    assert turns[1].tokens_in == 10
    assert turns[1].tokens_out == 20
    assert turns[1].model == "fake"
    assert job.status == "completed"
    assert job.completed_at is not None
    assert ws.last_sdk_session_id == "s1"


@pytest.mark.asyncio
async def test_process_turn_writes_tool_events(
    pg_session_factory,
    _settings_env,
):
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [
            AssistantTextEvent(text="Calling a tool."),
            ToolUseEvent(tool_name="X", tool_use_id="u1", input={"a": 1}),
            ToolResultEvent(tool_use_id="u1", content="result", is_error=False),
            AssistantTextEvent(text="Done."),
            TurnDoneEvent(tokens_in=5, tokens_out=15, model="fake", session_id="s2"),
        ],
    )

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        turns = list(
            s.scalars(
                select(ChatTurn).where(ChatTurn.workspace_id == wsid).order_by(ChatTurn.position)
            )
        )

    # user + assistant text + tool_use + tool_result + assistant text = 5 rows.
    assert [t.role for t in turns] == ["user", "assistant", "assistant", "tool", "assistant"]
    assert turns[2].content["type"] == "tool_use"
    assert turns[3].content["type"] == "tool_result"
    # Tokens land on the LAST assistant row.
    assert turns[-1].tokens_in == 5
    assert turns[-1].tokens_out == 15


@pytest.mark.asyncio
async def test_process_turn_handles_sdk_turn_error(
    pg_session_factory,
    _settings_env,
):
    """SDKTurnError mid-stream marks the turn_job failed and writes a
    cloud_turn_failed event."""
    adapter = FakeAgentAdapter()

    async def explode_after_one(handle, msg):
        yield AssistantTextEvent(text="started")
        raise SDKTurnError("rate limit", status="529")

    adapter.send_turn = explode_after_one  # type: ignore[method-assign]
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        job_id = job.id

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        events = list(s.scalars(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == wsid)))

    assert job.status == "failed"
    assert "529" in (job.error or "")
    failed_events = [e for e in events if e.kind == "cloud_turn_failed"]
    assert len(failed_events) == 1
    assert failed_events[0].payload["turn_job_id"] == str(job_id)
    assert "529" in failed_events[0].payload["error"]
    # B6-residuals: outcome events carry actor_id of the submitter so
    # compliance can read who-submitted-what off a single row.
    assert failed_events[0].actor_id == uid


@pytest.mark.asyncio
async def test_process_turn_writes_cloud_turn_completed_event(
    pg_session_factory,
    _settings_env,
):
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=10, tokens_out=5, model="fake", session_id="s3"),
        ],
    )

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_completed",
                )
            )
        )
        # Read the submitter back from the job row to assert the
        # outcome event carries the same actor_id (B6-residuals).
        job_row = s.get(TurnJob, job_id)
        submitter_id = job_row.submitted_by_user_id
    assert len(events) == 1
    payload = events[0].payload
    assert payload["tokens_in"] == 10
    assert payload["tokens_out"] == 5
    assert payload["turn_job_id"] == str(job_id)
    assert "content" not in payload
    assert events[0].actor_id == submitter_id


@pytest.mark.asyncio
async def test_process_turn_noop_if_already_running(
    pg_session_factory,
    _settings_env,
):
    """If the row was claimed by another worker (status='running' already),
    process_turn_once returns without raising or double-processing."""
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="s4"),
        ],
    )

    with pg_session_factory() as s:
        from datetime import UTC, datetime

        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        job.status = "running"
        job.started_at = datetime.now(UTC)
        s.commit()

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    # Status unchanged; no extra chat_turns written beyond the user one.
    # NB: the peek-status short-circuit returns BEFORE launch, so no
    # subprocess fires either — that's how the already-running row avoids
    # being clobbered by the orphan sweep that launch would otherwise run.
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        turns = list(s.scalars(select(ChatTurn).where(ChatTurn.workspace_id == wsid)))
    assert job.status == "running"
    assert len(turns) == 1  # just the user turn from submit


@pytest.mark.asyncio
async def test_process_turn_survives_stale_running_row_from_prior_crash(
    pg_session_factory,
    _settings_env,
):
    """Regression for H1: a stale `running` turn_jobs row (from a crashed
    prior process) must NOT block the new claim. The launch-first ordering
    + B3's orphan sweep cleans up the stale row before we try to claim,
    so the unique partial index doesn't fire."""
    from datetime import UTC, datetime

    from call_manager.config import Settings

    settings = Settings()
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

    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        # Stale `running` row from a hypothetical crashed prior process.
        s.add(
            TurnJob(
                workspace_id=ws.id,
                status="running",
                started_at=datetime.now(UTC),
            )
        )
        s.commit()
        uid, wsid = user.id, ws.id

    # Submit a NEW turn — this would IntegrityError on the unique partial
    # index if the stale row hadn't been swept first.
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        new_job_id = job.id

    # process_turn_once should: launch (sweep marks stale row failed),
    # then claim new_job_id, then dispatch.
    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=new_job_id,
    )

    with pg_session_factory() as s:
        all_jobs = list(
            s.scalars(
                select(TurnJob)
                .where(TurnJob.workspace_id == wsid)
                .order_by(TurnJob.queued_at.nulls_first())
            )
        )
    statuses = sorted([j.status for j in all_jobs])
    # One stale failed (from sweep), one completed (the new one).
    assert statuses == ["completed", "failed"]
    stale = next(j for j in all_jobs if j.status == "failed")
    assert stale.error == "orphaned"


@pytest.mark.asyncio
async def test_process_turn_leaves_row_queued_when_launch_raises(
    pg_session_factory,
    _settings_env,
):
    """If controller.launch() itself raises (broken adapter, missing key,
    LockNotAcquired in a multi-instance future), the processor must
    leave the row in `queued` state so the next drain tick retries.
    No claim, no chat_turns assistant rows, no cloud_turn_failed
    outcome event (the launch failure path is documented as audit-
    trail-incomplete; B6/B8 may surface after K retries)."""
    from call_manager.config import Settings
    from tests.proposal_assist.conftest import make_user

    settings = Settings()
    adapter = FakeAgentAdapter()

    async def boom(context):
        raise RuntimeError("adapter init failed")

    adapter.launch = boom  # type: ignore[method-assign]
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        job_id = job.id

    # process_turn_once must NOT raise; it must log and return.
    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        turns = list(s.scalars(select(ChatTurn).where(ChatTurn.workspace_id == wsid)))
        outcome_events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind.in_(["cloud_turn_completed", "cloud_turn_failed"]),
                )
            )
        )

    # Row stays queued for retry.
    assert job.status == "queued"
    assert job.started_at is None
    # Only the user turn from submit was written; no assistant rows.
    assert [t.role for t in turns] == ["user"]
    # No outcome event — documented limitation; the cloud_turn_sent from
    # submit is the only audit row.
    assert outcome_events == []


@pytest.mark.asyncio
async def test_process_turn_finalizes_failed_when_user_turn_row_missing(
    pg_session_factory,
    _settings_env,
):
    """If the linked chat_turns row was deleted between submit and
    processing, the processor marks the job failed via _finalize_failed
    (which writes cloud_turn_failed)."""
    from call_manager.config import Settings
    from tests.proposal_assist.conftest import make_user

    settings = Settings()
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id
    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        job_id = job.id
        user_turn_id = job.user_turn_id

    # Simulate the user turn being deleted between submit and process.
    with pg_session_factory() as s:
        ut = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        # Also detach from turn_job so FK doesn't cascade-block delete.
        j = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        j.user_turn_id = None
        s.flush()
        s.delete(ut)
        s.commit()

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        failed_events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_failed",
                )
            )
        )

    assert job.status == "failed"
    assert "missing_user_turn_row" in (job.error or "")
    assert len(failed_events) == 1
    assert "missing_user_turn_row" in failed_events[0].payload["error"]


@pytest.mark.asyncio
async def test_finalize_success_clears_user_turn_pre_count(
    pg_session_factory,
    _settings_env,
):
    """B7 submit pre-counts vision tokens on the USER chat_turns row.
    The processor writes the SDK's TRUE token count (which already
    includes input tokens) on the ASSISTANT chat_turns row. Without
    clearing the user-turn pre-count, the daily-cap query double-counts
    the user's input tokens.

    After _finalize_success runs, the user-turn row must have
    tokens_in = NULL so the cap sum reflects only the SDK-reported
    truth on the assistant row."""
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=137, tokens_out=42, model="fake", session_id="sX"),
        ],
    )

    # Simulate B7's submit-time pre-count by writing a non-NULL
    # tokens_in on the user-turn row BEFORE the drain.
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == job.user_turn_id))
        user_turn.tokens_in = 99
        s.commit()
        user_turn_id = user_turn.id

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        # User-turn's pre-count must be cleared.
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        assert user_turn.tokens_in is None, (
            "user-turn pre-count must be NULL after finalize; "
            "otherwise the cap query double-counts input tokens"
        )
        # Assistant row carries the SDK truth.
        assistant_turns = list(
            s.scalars(
                select(ChatTurn)
                .where(ChatTurn.workspace_id == wsid)
                .where(ChatTurn.role == "assistant")
            )
        )
        assert len(assistant_turns) == 1
        assert assistant_turns[0].tokens_in == 137
        assert assistant_turns[0].tokens_out == 42
