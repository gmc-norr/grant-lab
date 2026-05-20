"""Adapter contract for the drafting agent.

Two concrete adapters live in this package:
  - FakeAgentAdapter (fake_adapter.py) — scriptable test double
  - ClaudeAgentSDKAdapter (claude_adapter.py) — wraps claude-agent-sdk

Everything else in the codebase talks to a DraftingAgent — never directly
to the SDK. An SDK upgrade should touch only claude_adapter.py.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class AgentHandle:
    """Opaque token returned by launch(); passed back to send_turn /
    terminate. The `state` dict is adapter-internal (subprocess pid,
    recorder, etc.) — controller code MUST NOT introspect it."""

    workspace_id: uuid.UUID
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class LaunchContext:
    """Everything an adapter needs at session-creation time.

    Built by WorkspaceController.launch(); passed verbatim to
    DraftingAgent.launch(). Adapter implementations consume what they
    need (Claude adapter feeds resume_messages into the SDK session;
    Fake adapter records the whole context for assertions).
    """

    workspace_id: uuid.UUID
    resume_messages: list[dict] = field(default_factory=list)


# Stream event variants. send_turn yields these in order during a turn.
# B4 will persist these into chat_turns rows; B6 will forward them via SSE.


@dataclass
class AssistantTextEvent:
    """A chunk of assistant text (the SDK may stream multiple)."""

    text: str


@dataclass
class ToolUseEvent:
    """Agent invoked a tool. The adapter has already routed the call
    to the registered handler; this event is informational so the UI
    can show 'calling tool X...'."""

    tool_name: str
    tool_use_id: str
    input: dict[str, Any]


@dataclass
class ToolResultEvent:
    """Result of a tool invocation, fed back into the model loop."""

    tool_use_id: str
    content: str
    is_error: bool


@dataclass
class TurnDoneEvent:
    """Terminal event for a turn. Carries token + model telemetry so
    chat_turns rows can be stamped."""

    tokens_in: int | None
    tokens_out: int | None
    model: str | None


StreamEvent = AssistantTextEvent | ToolUseEvent | ToolResultEvent | TurnDoneEvent


@runtime_checkable
class DraftingAgent(Protocol):
    """The adapter contract.

    `launch` is awaited once per (workspace, idle-window). It does whatever
    initialization the adapter needs — for Claude this means connecting
    the SDK client and seeding it with context.resume_messages; for Fake
    it just records the call.

    `send_turn` is an async generator: caller iterates with `async for
    event in adapter.send_turn(handle, msg)`. The final event is always
    TurnDoneEvent.

    `terminate` is idempotent: calling on an already-torn-down handle is
    a no-op (logs but doesn't raise)."""

    async def launch(self, context: LaunchContext) -> AgentHandle: ...

    async def send_turn(
        self,
        handle: AgentHandle,
        user_message: str,
    ) -> AsyncIterator[StreamEvent]: ...

    async def terminate(self, handle: AgentHandle) -> None: ...
