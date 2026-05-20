"""Real Claude Agent SDK adapter.

This file is the only place in the codebase that imports claude_agent_sdk.
An SDK upgrade should touch only this file plus its test. The DraftingAgent
Protocol (contract.py) is stable.

Discovery (SDK 0.2.82):
- Entry point: ClaudeSDKClient (bidirectional, persistent subprocess)
- Options class: ClaudeAgentOptions
- Streaming types: AssistantMessage, UserMessage, SystemMessage, ResultMessage
  Content blocks: TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock,
                  ServerToolUseBlock, ServerToolResultBlock
- Lifecycle: connect() once at launch, disconnect() at terminate.
  The client maintains a persistent subprocess + anyio task group between
  connect() and disconnect(); do NOT wrap each turn in `async with` — that
  re-handshakes the subprocess every turn.
- Resume-seeding mechanism: ClaudeAgentOptions.resume accepts a session-ID
  string (UUID), not raw message dicts. There is no messages= kwarg on
  connect(). Seeding resume_messages is handled by replaying them as initial
  query() calls before returning the handle (see _seed_resume below).
  This means the resume is "soft" — the model sees the prior turns as new
  user/assistant exchanges rather than as a true transcript replay. A future
  SDK version that exposes a messages= seed parameter would be cleaner; track
  as a known limitation.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    AssistantTextEvent,
    LaunchContext,
    StreamEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)

logger = logging.getLogger(__name__)


class ClaudeAgentSDKAdapter:
    """Wraps claude-agent-sdk in a per-workspace persistent client.

    Lifecycle per workspace:
      - launch(context): construct + connect the SDK client, seeding it with
        context.resume_messages by replaying them as initial queries before
        returning the handle. The connected client is stored on
        AgentHandle.state['client'].
      - send_turn(handle, msg): calls client.query(msg) then iterates
        client.receive_response() for streaming events. No async-with wrapper
        — the client is already connected; re-wrapping would re-handshake the
        subprocess on every turn.
      - terminate(handle): calls client.disconnect() and discards the client
        from state. Safe to call even if client was never connected.
    """

    def __init__(
        self,
        *,
        model: str,
        workspaces_dir: str,
        permission_mode: str = "bypassPermissions",
    ) -> None:
        self._model = model
        self._workspaces_dir = Path(workspaces_dir)
        self._permission_mode = permission_mode  # type: ignore[assignment]

    async def launch(self, context: LaunchContext) -> AgentHandle:
        """Connect the SDK client for this workspace.

        Seeds resume_messages by replaying them as initial query/receive
        pairs before returning the handle. This is a workaround for the SDK
        not supporting a messages= seed parameter on connect().

        The repo_dir is passed as cwd so the Claude subprocess runs inside
        the workspace's git-backed repo directory. The directory is not
        created here — Phase 0 is responsible for that.
        """
        workspace_id = context.workspace_id
        repo_dir = self._workspaces_dir / str(workspace_id) / "repo"

        options = ClaudeAgentOptions(
            cwd=str(repo_dir),
            model=self._model,
            permission_mode=self._permission_mode,  # type: ignore[arg-type]
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()

        handle = AgentHandle(
            workspace_id=workspace_id,
            state={"client": client},
        )

        # Replay prior history before returning the handle so the first real
        # user turn has context. resume_messages is a list of
        # {"role": "user"|"assistant", "content": str|list} dicts produced by
        # resume.py. We replay only user turns; assistant turns are implied by
        # the model's responses during replay — sending them directly could
        # confuse the subprocess. Drain each response to keep the subprocess
        # in sync.
        #
        # NOTE: This is a "soft" replay — the model re-processes the history
        # rather than a true transcript injection. A native messages= seed
        # parameter would be preferable; track as a known limitation.
        for msg in context.resume_messages:
            role = msg.get("role", "")
            if role != "user":
                continue
            content = msg.get("content", "")
            text = content if isinstance(content, str) else _flatten_content(content)
            if not text:
                continue
            await self._drain_turn(client, text)

        return handle

    async def _drain_turn(self, client: ClaudeSDKClient, text: str) -> None:
        """Send a user message and drain all events (discard output)."""
        await client.query(text)
        async for _ in client.receive_response():
            pass

    async def send_turn(
        self,
        handle: AgentHandle,
        user_message: str,
    ) -> AsyncIterator[StreamEvent]:
        """Stream events for one turn.

        The client is already connected (from launch); do NOT wrap in
        `async with` — that re-connects and spawns a new subprocess.
        """
        client: ClaudeSDKClient = handle.state["client"]
        tokens_in: int | None = None
        tokens_out: int | None = None
        model: str | None = self._model

        await client.query(user_message)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                # Extract model name from the first AssistantMessage
                if message.model:
                    model = message.model
                # Extract token counts from usage dict
                if message.usage and tokens_in is None:
                    tokens_in = message.usage.get("input_tokens")
                    tokens_out = message.usage.get("output_tokens")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield AssistantTextEvent(text=block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield ToolUseEvent(
                            tool_name=block.name,
                            tool_use_id=block.id,
                            input=block.input,
                        )

            elif isinstance(message, UserMessage):
                # Tool results come back as UserMessage with ToolResultBlock content
                content = message.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            yield ToolResultEvent(
                                tool_use_id=block.tool_use_id,
                                content=_content_to_str(block.content),
                                is_error=bool(block.is_error),
                            )

            elif isinstance(message, ResultMessage):
                # Extract model-level usage from model_usage if present
                mu: dict[str, Any] | None = message.model_usage
                if mu is not None:
                    if tokens_in is None:
                        tokens_in = mu.get("input_tokens")
                    if tokens_out is None:
                        tokens_out = mu.get("output_tokens")
                yield TurnDoneEvent(
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    model=model,
                )
                return

        # Defensive: if the SDK never yielded a ResultMessage, emit a bare
        # TurnDoneEvent so the consumer's loop exits cleanly.
        yield TurnDoneEvent(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
        )

    async def terminate(self, handle: AgentHandle) -> None:
        """Disconnect the SDK client. Safe to call even if never connected."""
        client: ClaudeSDKClient | None = handle.state.pop("client", None)
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            logger.debug(
                "Ignoring exception during SDK client disconnect for workspace %s",
                handle.workspace_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _flatten_content(content: list[Any]) -> str:
    """Flatten a list of content blocks to a plain string.

    Handles the resume serializer format where content may be a list of
    {"type": "text", "text": "..."} dicts.
    """
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        elif isinstance(block, str):
            parts.append(block)
        else:
            parts.append(str(block))
    return " ".join(parts)


def _content_to_str(content: str | list[Any] | None) -> str:
    """Convert tool result content to a plain string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _flatten_content(content)
    return str(content)
