"""Real-SDK adapter tests. Skipped unless ANTHROPIC_API_KEY is set in
env — CI without a key shouldn't hit the API. The protocol-conformance
and unit tests run always (no SDK calls)."""

import os
import uuid

import pytest

from call_manager.proposal_assist.drafting.claude_adapter import (
    ClaudeAgentSDKAdapter,
)
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    DraftingAgent,
    LaunchContext,
    StreamEvent,
    TurnDoneEvent,
)

# ---------------------------------------------------------------------------
# M1 — _flatten_content unit tests (no SDK calls)
# ---------------------------------------------------------------------------


def test_flatten_content_handles_single_dict():
    """ToolResultBlock content may be a single text-block dict.
    _flatten_content must extract .text, not iterate the dict's keys."""
    from call_manager.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    assert _flatten_content({"type": "text", "text": "hello"}) == "hello"


def test_flatten_content_handles_list_of_dicts():
    from call_manager.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    blocks = [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ]
    assert _flatten_content(blocks) == "hello world"


def test_flatten_content_handles_string_passthrough():
    from call_manager.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    assert _flatten_content("hi") == "hi"


def test_flatten_content_returns_empty_for_non_text_block():
    """Non-text ToolResultBlock content (image, tool_use) is dropped
    (returns empty string)."""
    from call_manager.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    assert _flatten_content({"type": "image", "source": {}}) == ""
    assert _flatten_content({"type": "tool_use", "id": "x", "name": "y"}) == ""


# ---------------------------------------------------------------------------
# M2 — SDKTurnError on ResultMessage error result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_turn_raises_on_sdk_error_result():
    """A ResultMessage with is_error=True must raise SDKTurnError, not
    pass through as a successful TurnDoneEvent. B4 catches this and
    marks the turn failed."""
    from unittest.mock import AsyncMock, MagicMock

    from claude_agent_sdk.types import ResultMessage

    from call_manager.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
        SDKTurnError,
    )
    from call_manager.proposal_assist.drafting.contract import AgentHandle

    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )

    # Build a fake client that yields a single ResultMessage with is_error=True.
    fake_client = MagicMock()
    fake_client.query = AsyncMock()

    async def fake_receive():
        # Construct a ResultMessage with is_error=True. The SDK's actual
        # __init__ has many required fields — use object.__new__ to
        # bypass __init__ and set only what we need.
        rm = object.__new__(ResultMessage)
        rm.is_error = True
        rm.api_error_status = "529"
        rm.errors = ["overloaded"]
        rm.model_usage = None
        yield rm

    fake_client.receive_response = fake_receive

    handle = AgentHandle(
        workspace_id=uuid.uuid4(),
        state={"client": fake_client},
    )

    with pytest.raises(SDKTurnError, match="529"):
        async for _ in adapter.send_turn(handle, "hi"):
            pass


# ---------------------------------------------------------------------------
# B3 — native session-ID resume scaffolding (no SDK calls)
# ---------------------------------------------------------------------------


def test_launch_uses_resume_session_id_in_options(monkeypatch):
    """When LaunchContext.resume_session_id is set, the adapter passes
    it to ClaudeAgentOptions(resume=...) so the SDK natively resumes
    the prior conversation. Verified by spying on options construction."""
    import asyncio

    from call_manager.proposal_assist.drafting import claude_adapter as ca

    captured: dict[str, object] = {}
    real_options = ca.ClaudeAgentOptions

    def spy_options(**kw):
        captured.update(kw)
        return real_options(**kw)

    monkeypatch.setattr(ca, "ClaudeAgentOptions", spy_options)

    class _StubClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def disconnect(self):
            pass

    monkeypatch.setattr(ca, "ClaudeSDKClient", _StubClient)

    adapter = ca.ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )
    ctx = LaunchContext(
        workspace_id=uuid.uuid4(),
        resume_session_id="abc-123",
    )
    asyncio.run(adapter.launch(ctx))
    assert captured.get("resume") == "abc-123"


def test_launch_omits_resume_when_session_id_is_none(monkeypatch):
    """No resume_session_id → no resume= in options (fresh session)."""
    import asyncio

    from call_manager.proposal_assist.drafting import claude_adapter as ca

    captured: dict[str, object] = {}
    real_options = ca.ClaudeAgentOptions

    def spy_options(**kw):
        captured.update(kw)
        return real_options(**kw)

    monkeypatch.setattr(ca, "ClaudeAgentOptions", spy_options)

    class _StubClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def disconnect(self):
            pass

    monkeypatch.setattr(ca, "ClaudeSDKClient", _StubClient)

    adapter = ca.ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )
    ctx = LaunchContext(workspace_id=uuid.uuid4())
    asyncio.run(adapter.launch(ctx))
    assert "resume" not in captured


@pytest.mark.asyncio
async def test_send_turn_captures_session_id_on_turn_done():
    """ResultMessage.uuid (SDK 0.2.82 attribute name) flows through to
    TurnDoneEvent.session_id so B4 can persist it for native resume on
    the next launch."""
    from unittest.mock import AsyncMock, MagicMock

    from claude_agent_sdk.types import ResultMessage

    from call_manager.proposal_assist.drafting.contract import AgentHandle

    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )

    fake_client = MagicMock()
    fake_client.query = AsyncMock()

    async def fake_receive():
        rm = object.__new__(ResultMessage)
        rm.is_error = False
        rm.api_error_status = None
        rm.errors = None
        rm.model_usage = None
        # SDK 0.2.82 uses `uuid`; the adapter probes both names.
        rm.uuid = "session-abc-123"
        yield rm

    fake_client.receive_response = fake_receive

    handle = AgentHandle(workspace_id=uuid.uuid4(), state={"client": fake_client})

    events = []
    async for ev in adapter.send_turn(handle, "hi"):
        events.append(ev)

    done = next(ev for ev in events if isinstance(ev, TurnDoneEvent))
    assert done.session_id == "session-abc-123"


# ---------------------------------------------------------------------------
# Protocol conformance (no SDK calls)
# ---------------------------------------------------------------------------


def test_claude_adapter_implements_protocol():
    """No SDK calls — just checks the class satisfies the Protocol.
    Runs unconditionally (no API key needed)."""
    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/test_workspaces",
    )
    assert isinstance(adapter, DraftingAgent)


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="real SDK tests require ANTHROPIC_API_KEY",
)
@pytest.mark.asyncio
async def test_claude_adapter_can_round_trip_a_simple_turn():
    """End-to-end: launch, send a one-line turn, receive at least one
    AssistantTextEvent and a TurnDoneEvent. Uses Haiku for speed/cost."""
    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/test_workspaces",
    )
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)
    handle = await adapter.launch(ctx)
    try:
        events = []
        async for event in adapter.send_turn(handle, "Say 'hello'."):
            events.append(event)
        assert any(isinstance(e, AssistantTextEvent) for e in events)
        assert isinstance(events[-1], TurnDoneEvent)
    finally:
        await adapter.terminate(handle)


# ---------------------------------------------------------------------------
# B8 residuals 2 — vision-token undercount fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_turn_uses_result_message_authoritative_usage():
    """ResultMessage.usage is the SDK's final tally and MUST win over the
    running counts captured from AssistantMessage. Per a real round-trip
    probe (SDK 0.2.82), AssistantMessage.usage often has partial output
    (e.g. output_tokens=0 on the first streamed chunk), and the
    input_tokens field is just the un-cached portion — the bulk of
    Claude Code's input cost is in cache_creation_input_tokens
    (~3787 tokens for a 'say hi' prompt).

    Cap-relevant input = input_tokens + cache_creation_input_tokens +
                         cache_read_input_tokens
    (all components the model actually processed for input)."""
    from unittest.mock import AsyncMock, MagicMock

    from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

    from call_manager.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
    )
    from call_manager.proposal_assist.drafting.contract import AgentHandle

    adapter = ClaudeAgentSDKAdapter(
        model="claude-opus-4-7",
        workspaces_dir="/tmp/x",
    )
    fake_client = MagicMock()
    fake_client.query = AsyncMock()

    async def fake_receive():
        # First AssistantMessage: a STUB with output_tokens=0 (matches the
        # real-SDK streaming behavior where the first delta lands before
        # any text has been generated).
        am_stub = AssistantMessage(
            content=[],
            model="claude-opus-4-7",
            usage={"input_tokens": 6, "output_tokens": 0},
        )
        yield am_stub
        # Second AssistantMessage: the real text block. Even here, usage
        # reflects a running tally — input_tokens is still the un-cached
        # portion, and cache_creation_input_tokens (the bulk) is NOT in
        # AssistantMessage.usage at all in some SDK builds.
        am_real = AssistantMessage(
            content=[TextBlock(text="hi")],
            model="claude-opus-4-7",
            usage={"input_tokens": 6, "output_tokens": 7},
        )
        yield am_real
        # ResultMessage with the AUTHORITATIVE tally — including the
        # 3787 cache_creation_input_tokens that the AssistantMessage
        # path didn't surface.
        rm = object.__new__(ResultMessage)
        rm.is_error = False
        rm.api_error_status = None
        rm.errors = None
        rm.usage = {
            "input_tokens": 6,
            "cache_creation_input_tokens": 3787,
            "cache_read_input_tokens": 0,
            "output_tokens": 7,
        }
        rm.model_usage = {
            "claude-opus-4-7": {
                "inputTokens": 6,
                "outputTokens": 7,
                "cacheCreationInputTokens": 3787,
                "cacheReadInputTokens": 0,
            }
        }
        rm.session_id = "sess-abc"
        rm.uuid = "sess-abc"
        yield rm

    fake_client.receive_response = fake_receive
    handle = AgentHandle(workspace_id=uuid.uuid4(), state={"client": fake_client})

    events: list[StreamEvent] = []
    async for ev in adapter.send_turn(handle, "hi"):
        events.append(ev)

    done = [e for e in events if isinstance(e, TurnDoneEvent)][-1]
    # Cap-relevant input tokens = sum of all input components the model
    # actually processed for input: base + cache_creation + cache_read.
    # 6 + 3787 + 0 = 3793.
    assert done.tokens_in == 3793, (
        f"expected 3793 (sum of input + cache_creation + cache_read), got {done.tokens_in}"
    )
    assert done.tokens_out == 7
    assert done.model == "claude-opus-4-7"
    assert done.session_id == "sess-abc"


@pytest.mark.asyncio
async def test_send_turn_falls_back_to_assistant_usage_if_result_missing():
    """Defensive: if the SDK never yields a ResultMessage (network drop,
    early disconnect), we fall back to the AssistantMessage usage so
    the cap still gets *some* count rather than NULL."""
    from unittest.mock import AsyncMock, MagicMock

    from claude_agent_sdk.types import AssistantMessage, TextBlock

    from call_manager.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
    )
    from call_manager.proposal_assist.drafting.contract import AgentHandle

    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )
    fake_client = MagicMock()
    fake_client.query = AsyncMock()

    async def fake_receive():
        # Only an AssistantMessage; no ResultMessage at all.
        yield AssistantMessage(
            content=[TextBlock(text="bye")],
            model="claude-haiku-4-5",
            usage={"input_tokens": 100, "output_tokens": 50},
        )

    fake_client.receive_response = fake_receive
    handle = AgentHandle(workspace_id=uuid.uuid4(), state={"client": fake_client})

    events: list[StreamEvent] = []
    async for ev in adapter.send_turn(handle, "hi"):
        events.append(ev)

    done = [e for e in events if isinstance(e, TurnDoneEvent)][-1]
    # Without ResultMessage, the best we have is AssistantMessage.usage.
    # input_tokens=100, no cache_* fields → tokens_in=100.
    assert done.tokens_in == 100
    assert done.tokens_out == 50


# ---------------------------------------------------------------------------
# Subprocess hardening (no SDK calls): secret-scrub env + setting_sources=[]
# ---------------------------------------------------------------------------


def test_build_options_disables_filesystem_settings():
    """setting_sources=[] keeps the bypassPermissions agent from loading
    project/user .claude/settings.json (hooks, permission grants, MCP defs)."""
    adapter = ClaudeAgentSDKAdapter(model="claude-haiku-4-5", workspaces_dir="/tmp/x")
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    assert kwargs["setting_sources"] == []
    # The existing built-in-toolbox lockdown must remain.
    assert kwargs["tools"] == []
    assert kwargs["strict_mcp_config"] is True


def test_build_options_scrubs_app_secrets_from_env():
    """App secrets are neutralized (empty) in the subprocess env; the SDK
    merges options.env over the inherited environment, so empty == removed."""
    adapter = ClaudeAgentSDKAdapter(model="claude-haiku-4-5", workspaces_dir="/tmp/x")
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    env = kwargs["env"]
    for key in ("DATABASE_URL", "SESSION_SECRET", "RESEND_API_KEY", "SMTP_PASSWORD"):
        assert env[key] == ""
    # The agent IS a Claude client — its own API key must NOT be scrubbed.
    assert "ANTHROPIC_API_KEY" not in env


def test_build_options_appends_reference_safety_system_prompt():
    """A standing system instruction tells the agent to treat
    <untrusted-reference> content as data, not instructions. Uses the preset
    form so the default Claude Code system prompt is preserved and appended.
    The append string now also includes the citation instruction; we check that
    the safety phrases are present and the preset fields are correct."""
    adapter = ClaudeAgentSDKAdapter(model="claude-haiku-4-5", workspaces_dir="/tmp/x")
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    sp = kwargs["system_prompt"]
    assert sp["type"] == "preset"
    assert sp["preset"] == "claude_code"
    assert "untrusted-reference" in sp["append"]
    assert "never as instructions" in sp["append"]


def test_build_options_appends_citation_instruction():
    """The system-prompt append also includes the citation instruction so the
    agent knows to use insert_citation / validate_citations / list_unsupported_claims
    and to copy a verbatim evidence_quote from the supporting chunk."""
    from call_manager.proposal_assist.drafting import claude_adapter as ca

    adapter = ClaudeAgentSDKAdapter(model="claude-haiku-4-5", workspaces_dir="/tmp/x")
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    sp = kwargs["system_prompt"]
    append = sp["append"]
    assert "insert_citation" in append
    assert "validate_citations" in append
    assert "evidence_quote" in append or "verbatim" in append
    # The citation instruction must be non-empty and distinct from the safety one.
    assert ca._CITATION_INSTRUCTION
    assert ca._CITATION_INSTRUCTION != ca._REFERENCE_SAFETY_INSTRUCTION
    # Both instructions must be present in the combined append string.
    assert ca._REFERENCE_SAFETY_INSTRUCTION in append
    assert ca._CITATION_INSTRUCTION in append
