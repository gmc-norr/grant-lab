import uuid
from collections.abc import AsyncIterator

from grant_lab.proposal_assist.drafting.contract import (
    AgentHandle,
    AssistantTextEvent,
    DraftingAgent,
    LaunchContext,
    StreamEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)


def test_agent_handle_carries_workspace_id_and_arbitrary_state():
    """AgentHandle is a small dataclass: workspace_id (uuid) + opaque
    `state` dict for adapter-specific bookkeeping (process pid for
    Claude, recorder for Fake, etc.)."""
    wsid = uuid.uuid4()
    h = AgentHandle(workspace_id=wsid, state={"pid": 12345})
    assert h.workspace_id == wsid
    assert h.state["pid"] == 12345


def test_agent_handle_state_defaults_to_empty_dict():
    """Convenience: most adapters use state — default to {} so callers
    don't need to pass it on every construction."""
    wsid = uuid.uuid4()
    h = AgentHandle(workspace_id=wsid)
    assert h.state == {}


def test_launch_context_carries_workspace_id_and_resume_session_id():
    """LaunchContext is built by WorkspaceController.launch and passed
    verbatim to DraftingAgent.launch. When resume_session_id is set, the
    Claude adapter passes it to ClaudeAgentOptions(resume=...) so the SDK
    natively rehydrates the prior conversation — no replay needed."""
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid, resume_session_id="abc-123")
    assert ctx.workspace_id == wsid
    assert ctx.resume_session_id == "abc-123"


def test_launch_context_default_resume_session_id_is_none():
    """Fresh workspace (no prior session yet) — resume_session_id defaults
    to None. The adapter starts a fresh subprocess with no conversation
    history."""
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)
    assert ctx.workspace_id == wsid
    assert ctx.resume_session_id is None


def test_stream_event_types_carry_expected_fields():
    """The four event variants the contract guarantees."""
    a = AssistantTextEvent(text="hello")
    assert a.text == "hello"

    u = ToolUseEvent(
        tool_name="edit_proposal",
        tool_use_id="tu_1",
        input={"old": "foo", "new": "bar"},
    )
    assert u.tool_name == "edit_proposal"
    assert u.tool_use_id == "tu_1"
    assert u.input == {"old": "foo", "new": "bar"}

    r = ToolResultEvent(tool_use_id="tu_1", content="ok", is_error=False)
    assert r.is_error is False

    d = TurnDoneEvent(tokens_in=100, tokens_out=200, model="claude-opus-4-7")
    assert d.tokens_in == 100
    assert d.tokens_out == 200
    assert d.model == "claude-opus-4-7"
    assert d.session_id is None  # default

    d2 = TurnDoneEvent(tokens_in=1, tokens_out=2, model="m", session_id="s-xyz")
    assert d2.session_id == "s-xyz"


def test_drafting_agent_protocol_methods_exist():
    """The Protocol declares launch(context) / send_turn / terminate.
    Anything that quacks like it satisfies isinstance check
    (runtime_checkable)."""

    class _Dummy:
        async def launch(self, context: LaunchContext) -> AgentHandle:
            return AgentHandle(workspace_id=context.workspace_id)

        async def send_turn(
            self,
            handle: AgentHandle,
            user_message: str,
        ) -> AsyncIterator[StreamEvent]:
            return  # type: ignore[return-value]

        async def terminate(self, handle: AgentHandle) -> None:
            return None

        async def set_turn_provenance(
            self,
            workspace_id,
            *,
            triggering_turn_id,
            triggering_prompt,
            created_by,
            origin_session_id,
            lifecycle_generation,
            active_turn_job_id,
            allowed_tool_names=None,
            turn_selection=None,
        ) -> None: ...

    assert isinstance(_Dummy(), DraftingAgent)
