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
    DraftingAgent.launch().

    resume_session_id is the SDK session UUID returned by the previous
    successful turn's ResultMessage. When set, the adapter passes it to
    ClaudeAgentOptions(resume=...) so the SDK natively rehydrates prior
    conversation history without us replaying turns. When None, the
    subprocess starts fresh — the model has no memory of past turns.

    B4 will: capture session_id from TurnDoneEvent, persist it per
    workspace, and pass it back here on the next launch.
    """

    workspace_id: uuid.UUID
    resume_session_id: str | None = None


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
    """Terminal event for a turn. Carries token + model telemetry +
    SDK session id so chat_turns rows can be stamped and the workspace
    can persist the session id for the next launch's native resume."""

    tokens_in: int | None
    tokens_out: int | None
    model: str | None
    session_id: str | None = None


StreamEvent = AssistantTextEvent | ToolUseEvent | ToolResultEvent | TurnDoneEvent


class SDKTurnError(RuntimeError):
    """Raised by an adapter's send_turn when the backend signals an error
    result (rate-limit, 5xx, etc.) instead of completing the turn. Caller
    (B4's turn processor) catches this to mark the turn as failed without
    tearing down the handle. Lives on the contract so callers can catch
    it without importing claude_adapter (which would force eager SDK
    import)."""

    def __init__(self, message: str, *, status: str | None = None) -> None:
        super().__init__(message)
        self.status = status


class DraftingAtCapacity(RuntimeError):
    """Raised by the registry when a NET-NEW subprocess launch is requested but
    the live-subprocess ceiling (DRAFTING_MAX_LIVE_SUBPROCESSES) is reached and
    every live handle is mid-turn (so none can be evicted to make room).

    This is a transient "busy" signal: the API layer maps it to a retriable
    HTTP 429, and the drain tick leaves the queued turn for the next tick.
    Distinct from LockNotAcquired (cross-process contention) — this is the
    in-process self-imposed cap that keeps drafting from starving the engine's
    request pool. Lives on the contract so callers can catch it without
    importing the registry's concrete classes."""


class WorkspaceRetired(RuntimeError):
    """The workspace is retired (soft-deleted), so live per-workspace state may
    not be created for it.

    Two families of raise site, both installed by Slice B-retire:

    * TURNS — ``SubprocessRegistry.claim_launch_or_wait`` /
      ``fulfill_pending_launch`` and ``WorkspaceController.launch``. Turn-path
      callers finalize instead of sending.
    * COLLAB — ``RoomManager.acquire`` / ``operate``, including the re-checks
      after the out-of-lock ``await load()``. The WS route maps this to close
      1008; on HTTP routes there is no local ``except``, and main.py's
      app-level handler turns it into the same 404 as WorkspaceDeleted (which
      it is NOT a subclass of — both are bare RuntimeErrors).

    NOTE: this is a linearization point for CREATION, not a race-free gate on
    everything — a deletion committing after the check can still meet an
    in-flight streaming query, and the reconciliation sweep remains the retry."""

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"workspace {workspace_id} is retired")
        self.workspace_id = workspace_id


class AgentSdkConfigDirError(RuntimeError):
    """The app-owned CLAUDE_CONFIG_DIR is not a directory this app owns.

    Raised by ``claude_adapter.ensure_agent_sdk_config_dir`` instead of
    chmod'ing or handing the CLI a path the app cannot prove it owns: a symlink
    planted at that name (any of its four shapes — to a directory, to a file,
    dangling, or out of the tree), a plain file there or at one of its parents,
    or a leaf that still does not resolve to itself after creation. Fail closed:
    a launch that cannot prove where the CLI will keep its credentials must not
    start.

    NON-RETRYABLE, and that is why it lives on the contract rather than in
    ``claude_adapter``: it depends only on the filesystem shape at
    ``<workspaces_dir>/.agent-sdk-config``, which no number of drain ticks
    changes, so ``turns/processor.py`` must terminalize the turn instead of
    leaving it ``queued`` for a 5-second retry loop (review 2026-10-26, F4 and
    its re-review). The processor cannot import ``claude_adapter`` to catch it —
    that module is the ONE place the codebase imports ``claude_agent_sdk``, and
    the match build edition depends on the drain path not pulling the SDK in —
    which is the same reason ``SDKTurnError`` lives here. ``claude_adapter``
    re-exports the name, so every existing import of it keeps working.
    """


@runtime_checkable
class DraftingAgent(Protocol):
    """The adapter contract.

    `launch` is awaited once per (workspace, idle-window). It does whatever
    initialization the adapter needs — for Claude this means connecting
    the SDK client (optionally resuming a prior session via
    context.resume_session_id); for Fake it just records the call.

    `send_turn` is an async generator: caller iterates with `async for
    event in adapter.send_turn(handle, msg)`. The final event is always
    TurnDoneEvent.

    `user_message` is either a plain string (cheap text-only path) or a
    list of Anthropic content-block dicts (text and/or image with a
    base64 source). The dicts are ALREADY in Anthropic API shape —
    adapters MUST NOT do attachment-id resolution. That lives in the
    attachments/resolver module called by the processor.

    `terminate` is idempotent: calling on an already-torn-down handle is
    a no-op (logs but doesn't raise)."""

    async def launch(self, context: LaunchContext) -> AgentHandle: ...

    async def send_turn(
        self,
        handle: AgentHandle,
        user_message: str | list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]: ...

    async def terminate(self, handle: AgentHandle) -> None: ...

    # OPTIONAL (intentionally NOT a Protocol member, so runtime_checkable
    # isinstance() doesn't require it): adapters MAY provide
    #
    #     async def interrupt(self, handle: AgentHandle) -> None
    #
    # for a best-effort graceful stop of an in-flight turn WITHOUT tearing down
    # the subprocess (so a timed-out turn can stop cleanly and the handle be
    # reused). The registry/controller probe for it via getattr and skip when
    # absent (the Fake doesn't implement it). Callers MUST treat it as advisory
    # and keep terminate() as the guaranteed backstop.

    async def set_turn_provenance(
        self,
        workspace_id: uuid.UUID,
        *,
        triggering_turn_id: uuid.UUID | None,
        triggering_prompt: str | None,
        created_by: int | None,
        origin_session_id: str | None,
        lifecycle_generation: int,
        active_turn_job_id: uuid.UUID,
        allowed_tool_names: set[str] | None = None,
        turn_selection: dict | None = None,
    ) -> None:
        """Stamp server-trusted provenance onto the workspace's retained
        ToolContext before the next send_turn. Called UNCONDITIONALLY every turn
        by process_turn_once (the subprocess is reused across turns, so a missed
        stamp would leak the prior turn's provenance into the propose_* tools).
        A no-op if the workspace has no live handle/ctx (the next launch rebuilds
        the ctx, and the processor re-stamps before send_turn anyway)."""
        ...
