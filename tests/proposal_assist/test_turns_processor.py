import pytest
from sqlalchemy import select

from grant_lab.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
from grant_lab.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    SDKTurnError,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)
from grant_lab.proposal_assist.drafting.controller import WorkspaceController
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry
from grant_lab.proposal_assist.turns.processor import process_turn_once
from grant_lab.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


def _build_controller_and_submit(pg_session_factory, _settings_env, events):
    from grant_lab.config import Settings

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

    from grant_lab.config import Settings

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

    from grant_lab.config import Settings

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
    from grant_lab.config import Settings
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
    from grant_lab.config import Settings
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


@pytest.mark.asyncio
async def test_finalize_success_persists_tokens_on_user_turn_when_no_assistant_text(
    pg_session_factory,
    _settings_env,
):
    """Cap-bypass regression: when a turn produces no assistant row to
    stamp (last_assistant_id is None — e.g. only a ToolResultEvent and
    then done, with no AssistantTextEvent or ToolUseEvent), the SDK-
    reported tokens were previously dropped AND the user-turn pre-count
    cleared, so the turn vanished from the daily-cap sum. The tokens
    must instead be persisted onto the user-turn row so the cap still
    accounts for the turn."""
    adapter = FakeAgentAdapter()

    async def tool_only(handle, msg):
        # No AssistantTextEvent and no ToolUseEvent — neither sets
        # last_assistant_id, so it stays None at finalize time.
        yield ToolResultEvent(tool_use_id="u1", content="result", is_error=False)
        yield TurnDoneEvent(tokens_in=200, tokens_out=80, model="fake", session_id="sTool")

    adapter.send_turn = tool_only  # type: ignore[method-assign]
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    from grant_lab.config import Settings

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
        user_turn_id = job.user_turn_id

    # Simulate a submit-time pre-count on the user-turn row.
    with pg_session_factory() as s:
        ut = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        ut.tokens_in = 50
        s.commit()

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        assistant_turns = list(
            s.scalars(
                select(ChatTurn)
                .where(ChatTurn.workspace_id == wsid)
                .where(ChatTurn.role == "assistant")
            )
        )

    assert job.status == "completed"
    # No assistant row was written at all (ToolResultEvent writes a
    # role='tool' row, not an assistant row).
    assert assistant_turns == []
    # The SDK-reported tokens land on the user-turn row instead of being
    # dropped — the daily cap still counts this turn.
    assert user_turn.tokens_in == 200
    assert user_turn.tokens_out == 80
    assert user_turn.model == "fake"


@pytest.mark.asyncio
async def test_finalize_failed_clears_user_turn_pre_count(
    pg_session_factory,
    _settings_env,
):
    """A turn that fails never produced a real response, so its submit-
    time pre-count must not keep counting against the daily cap.
    _finalize_failed must clear the user-turn tokens_in (mirroring
    _finalize_success)."""
    adapter = FakeAgentAdapter()

    async def explode(handle, msg):
        yield AssistantTextEvent(text="started")
        raise SDKTurnError("boom", status="500")

    adapter.send_turn = explode  # type: ignore[method-assign]
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )

    from grant_lab.config import Settings

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
        user_turn_id = job.user_turn_id

    with pg_session_factory() as s:
        ut = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))
        ut.tokens_in = 123
        s.commit()

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        user_turn = s.scalar(select(ChatTurn).where(ChatTurn.id == user_turn_id))

    assert job.status == "failed"
    assert user_turn.tokens_in is None, (
        "failed turn must clear its user-turn pre-count so it stops counting against the daily cap"
    )


@pytest.mark.asyncio
async def test_process_turn_stamps_provenance_on_adapter(
    pg_session_factory,
    _settings_env,
):
    """process_turn_once stamps server-trusted provenance via
    controller.set_turn_provenance after loading the message and before
    send_turn: triggering_turn_id = the user ChatTurn id, created_by =
    submitted_by, triggering_prompt = the raw typed text, origin_session_id =
    None on a fresh (never-resumed) session."""
    wsid, job_id, controller, adapter = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [TurnDoneEvent(tokens_in=1, tokens_out=1, model="fake", session_id="s1")],
    )

    with pg_session_factory() as s:
        user_turn = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "user")
        )
        user_turn_id = user_turn.id

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    assert len(adapter.provenance) == 1
    ws_arg, ttid, prompt, created_by, origin_session_id = adapter.provenance[0]
    assert ws_arg == wsid
    assert ttid == user_turn_id
    assert prompt == "Hi"  # the raw typed text submitted in _build_controller_and_submit
    assert created_by is not None  # the submitter
    assert origin_session_id is None  # fresh session, never resumed
    # The stamp must land AFTER launch (the cold-launch ordering the plan fixed):
    # the workspace was already launched when set_turn_provenance ran. If the
    # processor ever reorders the stamp before launch, this flips to [False].
    assert adapter.provenance_saw_launch == [True]


def _set_user_turn_estimate(pg_session_factory, job_id, tokens_in):
    """Pin a known submit-time estimate on the job's user turn; return its id."""
    with pg_session_factory() as s:
        ut_id = s.scalar(select(TurnJob.user_turn_id).where(TurnJob.id == job_id))
        s.get(ChatTurn, ut_id).tokens_in = tokens_in
        s.commit()
    return ut_id


@pytest.mark.asyncio
async def test_retired_before_send_finalizes_without_sending_and_keeps_estimate(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    """Deletion between claim and the post-claim launch: no send, row not left
    'running', and the retained estimate is NOT cleared (no usage was reported)."""
    from tests.workspace_delete_helper import soft_delete_workspace_for_test

    _wsid, job_id, controller, adapter = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [TurnDoneEvent(tokens_in=10, tokens_out=20, model="fake", session_id="s1")],
    )
    ut_id = _set_user_turn_estimate(pg_session_factory, job_id, 1234)

    calls = {"n": 0}
    real_launch = controller.launch

    async def _launch(ws_id, **kwargs):
        # The deletion lands between the atomic claim and the post-claim launch:
        # it terminalizes the now-running row (retaining the estimate), and the
        # real precheck then raises WorkspaceRetired on the way back in.
        # **kwargs forwards protect_turn_job_id, which the post-claim launch
        # passes so the orphan sweep spares its own live row.
        calls["n"] += 1
        if calls["n"] >= 2:
            with pg_session_factory() as s:
                if s.get(Workspace, ws_id).soft_deleted_at is None:
                    soft_delete_workspace_for_test(s, workspace_id=ws_id)
        return await real_launch(ws_id, **kwargs)

    monkeypatch.setattr(controller, "launch", _launch)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "running"
        assert s.get(ChatTurn, ut_id).tokens_in == 1234  # estimate PRESERVED
    assert adapter.turns == []  # nothing sent


@pytest.mark.asyncio
async def test_retired_before_claim_cancels_the_queued_turn_instead_of_looping(
    pg_session_factory,
    _settings_env,
):
    """The PRE-claim launch (before the row is claimed) also raises WorkspaceRetired.

    Stamping soft_deleted_at directly — rather than calling soft_delete_workspace —
    is the point: it reproduces the one state the delete service normally prevents,
    a QUEUED turn on a deleted workspace. Leaving it queued (the generic
    launch-failed branch) would make every later drain tick re-raise and log a
    stack trace for a workspace that is never coming back.
    """
    from datetime import UTC, datetime

    _wsid, job_id, controller, adapter = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [TurnDoneEvent(tokens_in=10, tokens_out=20, model="fake", session_id="s1")],
    )
    with pg_session_factory() as s:
        s.get(Workspace, _wsid).soft_deleted_at = datetime.now(UTC)
        s.commit()

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "cancelled"  # NOT left queued
    assert adapter.turns == []  # never launched, never sent

    # And it stays terminal: a later tick is a plain no-op, not a second write.
    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )
    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "cancelled"
        cancels = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == _wsid,
                WorkspaceEvent.kind == "cloud_turn_cancelled",
            )
        ).all()
        assert len(cancels) == 1  # exactly one outcome event, not one per tick


@pytest.mark.asyncio
async def test_delete_during_stream_reconciles_reported_usage(
    pg_session_factory,
    _settings_env,
):
    """The real race: deletion terminalizes the row WHILE streaming. TurnDoneEvent
    arrives normally, _finalize_success loses the row and rolls back its stamp —
    so the usage must be reconciled instead of lost."""
    from sqlalchemy import func

    from tests.workspace_delete_helper import soft_delete_workspace_for_test

    ws_id, job_id, controller, _adapter = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [TurnDoneEvent(tokens_in=700, tokens_out=90, model="m", session_id=None)],
    )
    ut_id = _set_user_turn_estimate(pg_session_factory, job_id, 1234)

    # Script a stream that deletes the workspace mid-flight, then reports usage.
    class _DeletingStream:
        def __init__(self, ws_id):
            self.ws_id = ws_id

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            with pg_session_factory() as s:
                soft_delete_workspace_for_test(s, workspace_id=self.ws_id)
            yield TurnDoneEvent(tokens_in=700, tokens_out=90, model="m", session_id=None)

    controller.send_turn = lambda ws, msg: _DeletingStream(ws_id)

    await process_turn_once(
        session_factory=pg_session_factory,
        controller=controller,
        turn_job_id=job_id,
    )

    with pg_session_factory() as s:
        job = s.get(TurnJob, job_id)
        assert job.status != "completed"  # delete won the row
        # The reported usage was reconciled rather than rolled back with the finalize.
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceEvent)
                .where(
                    WorkspaceEvent.workspace_id == ws_id,
                    WorkspaceEvent.kind == "cloud_turn_usage_reconciled",
                )
            )
            == 1
        )
        ut = s.get(ChatTurn, ut_id)
        assert ut.tokens_in == 700 and ut.tokens_out == 90  # truth replaced the estimate
