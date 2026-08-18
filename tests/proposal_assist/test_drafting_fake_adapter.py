import uuid

from call_manager.proposal_assist.drafting.contract import (
    AgentHandle,
    AssistantTextEvent,
    DraftingAgent,
    LaunchContext,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.fake_adapter import (
    FakeAgentAdapter,
)


async def _collect(stream):
    """Async-for the stream into a list."""
    return [event async for event in stream]


async def test_fake_adapter_implements_protocol():
    fake = FakeAgentAdapter()
    assert isinstance(fake, DraftingAgent)


async def test_fake_adapter_records_launch_context():
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)
    handle = await fake.launch(ctx)
    assert isinstance(handle, AgentHandle)
    assert handle.workspace_id == wsid
    assert len(fake.launches) == 1
    assert fake.launches[0].workspace_id == wsid


async def test_fake_adapter_records_resume_session_id_on_handle_state():
    """The Claude adapter passes resume_session_id to ClaudeAgentOptions; the
    Fake stashes it on the handle so tests can assert what context was
    passed through."""
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid, resume_session_id="session-abc-123")
    handle = await fake.launch(ctx)
    assert handle.state["resume_session_id"] == "session-abc-123"


async def test_fake_adapter_scripts_turn_events():
    """Test scripts the events a turn will produce; adapter replays them
    in order. Final event is always TurnDoneEvent."""
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))

    fake.script_next_turn(
        [
            AssistantTextEvent(text="Hello, world!"),
            TurnDoneEvent(tokens_in=10, tokens_out=20, model="fake-model"),
        ]
    )

    events = await _collect(fake.send_turn(handle, "user message"))
    assert len(events) == 2
    assert isinstance(events[0], AssistantTextEvent)
    assert events[0].text == "Hello, world!"
    assert isinstance(events[1], TurnDoneEvent)


async def test_fake_adapter_records_turn_history():
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))
    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m")])
    fake.script_next_turn([TurnDoneEvent(tokens_in=2, tokens_out=2, model="m")])

    await _collect(fake.send_turn(handle, "first"))
    await _collect(fake.send_turn(handle, "second"))

    assert fake.turns == [(handle, "first"), (handle, "second")]


async def test_fake_adapter_terminate_is_idempotent():
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))
    await fake.terminate(handle)
    await fake.terminate(handle)  # second call doesn't raise
    assert fake.terminations == [handle, handle]


async def test_fake_adapter_send_turn_without_script_returns_terminal_only():
    """If a test forgot to script a turn, default to a TurnDoneEvent so
    the harness doesn't hang on `async for`."""
    fake = FakeAgentAdapter()
    wsid = uuid.uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))
    events = await _collect(fake.send_turn(handle, "unscripted"))
    assert len(events) == 1
    assert isinstance(events[0], TurnDoneEvent)


async def test_fake_adapter_records_set_turn_provenance():
    """set_turn_provenance is part of the DraftingAgent Protocol; the Fake
    records each call so the processor-stamp test can assert what was passed."""
    import uuid as _uuid

    fake = FakeAgentAdapter()
    wsid = _uuid.uuid4()
    ttid = _uuid.uuid4()
    await fake.set_turn_provenance(
        wsid,
        triggering_turn_id=ttid,
        triggering_prompt="hello agent",
        created_by=7,
        origin_session_id="sess-1",
        lifecycle_generation=1,
        active_turn_job_id=_uuid.uuid4(),
    )
    assert fake.provenance == [
        (wsid, ttid, "hello agent", 7, "sess-1"),
    ]
