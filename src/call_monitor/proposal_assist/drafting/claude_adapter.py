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
- Native resume: ClaudeAgentOptions(resume=<session_uuid_str>) tells the SDK
  subprocess to natively rehydrate a prior conversation. No history replay
  needed. The session UUID is obtained from ResultMessage.uuid (the attribute
  is named `uuid`, not `session_id`, in SDK 0.2.82; captured defensively via
  getattr with both names). B4 will persist this per workspace so subsequent
  launches can supply it via LaunchContext.resume_session_id.

Lifecycle per workspace:
  - launch(context): construct + connect the SDK client. If
    context.resume_session_id is set, pass it to ClaudeAgentOptions(resume=...)
    so the SDK natively resumes the prior session. Otherwise the subprocess
    starts fresh — the model has no memory of past turns.
  - send_turn(handle, msg): calls client.query(msg) then iterates
    client.receive_response() for streaming events. Captures session_id from
    ResultMessage.uuid (SDK 0.2.82 naming) and emits it on TurnDoneEvent so
    B4 can persist it.
  - terminate(handle): calls client.disconnect() and discards the client
    from state. Safe to call even if client was never connected.
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
    SDKTurnError,
    StreamEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)

logger = logging.getLogger(__name__)

__all__ = ["ClaudeAgentSDKAdapter", "SDKTurnError"]


class ClaudeAgentSDKAdapter:
    """Wraps claude-agent-sdk in a per-workspace persistent client.

    See module docstring for the full lifecycle description.
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

        If context.resume_session_id is set, asks the SDK to resume that
        session natively (ClaudeAgentOptions(resume=...)). Otherwise the
        subprocess starts fresh — the model has no memory of past turns.

        The repo_dir is passed as cwd so the Claude subprocess runs inside
        the workspace's git-backed repo directory. The directory is not
        created here — Phase 0 is responsible for that.
        """
        workspace_id = context.workspace_id
        repo_dir = self._workspaces_dir / str(workspace_id) / "repo"

        options_kwargs: dict[str, Any] = dict(
            cwd=str(repo_dir),
            model=self._model,
            permission_mode=self._permission_mode,  # type: ignore[typeddict-item]
        )
        if context.resume_session_id is not None:
            options_kwargs["resume"] = context.resume_session_id
        options = ClaudeAgentOptions(**options_kwargs)

        client = ClaudeSDKClient(options=options)
        await client.connect()

        return AgentHandle(
            workspace_id=workspace_id,
            state={"client": client},
        )

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
                                content=_flatten_content(block.content),
                                is_error=bool(block.is_error),
                            )

            elif isinstance(message, ResultMessage):
                # Check for error states before treating as success.
                # getattr() guards against future SDK field-name drift.
                # ResultMessage fields (SDK 0.2.82): is_error: bool,
                # api_error_status: int | None, errors: list[str] | None.
                is_error = bool(getattr(message, "is_error", False))
                api_status = getattr(message, "api_error_status", None)
                err_list = getattr(message, "errors", None)
                if is_error or api_status or err_list:
                    detail = f"is_error={is_error} status={api_status!r} errors={err_list!r}"
                    raise SDKTurnError(
                        f"SDK reported error for workspace {handle.workspace_id}: {detail}",
                        status=str(api_status) if api_status else None,
                    )

                # Capture model telemetry and the SDK session_id so B4 can persist
                # it on the workspace and pass it back via LaunchContext for the
                # next launch's native resume.
                # NOTE: in SDK 0.2.82 the attribute is named `uuid`, not `session_id`.
                # We probe both so a future SDK rename is handled automatically.
                mu: dict[str, Any] | None = getattr(message, "model_usage", None)
                if mu is not None:
                    if tokens_in is None:
                        tokens_in = mu.get("input_tokens")
                    if tokens_out is None:
                        tokens_out = mu.get("output_tokens")
                session_id: str | None = getattr(message, "session_id", None) or getattr(
                    message, "uuid", None
                )

                yield TurnDoneEvent(
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    model=model,
                    session_id=session_id,
                )
                return

        # Defensive: if the SDK never yielded a ResultMessage, emit a bare
        # TurnDoneEvent so the consumer's loop exits cleanly.
        yield TurnDoneEvent(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            session_id=None,
        )

    async def terminate(self, handle: AgentHandle) -> None:
        """Disconnect the SDK client. Safe to call even if never connected."""
        client: ClaudeSDKClient | None = handle.state.pop("client", None)
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            logger.warning(
                "SDK client disconnect failed for workspace %s; subprocess may have leaked",
                handle.workspace_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _flatten_content(content: Any) -> str:
    """Flatten tool result content to a plain string for ToolResultEvent.

    ToolResultBlock.content may be str, list, or None. Returns empty string
    for non-text blocks (image, tool_use) — those are intentionally dropped.

    Unification note: this function previously also served the soft-replay
    path (dropped in B3). It now exists solely for ToolResultBlock.content
    handling in send_turn().
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return ""  # non-text single block (image, tool_use, etc.) — dropped
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            piece = _flatten_content(block)
            if piece:
                parts.append(piece)
        return " ".join(parts)
    return str(content)
