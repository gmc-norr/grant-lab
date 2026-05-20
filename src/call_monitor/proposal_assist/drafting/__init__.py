"""Drafting package public surface.

External callers (B4 turn queue, B6 web layer) import from here, not from
specific submodules. The only file in this package that imports
claude_agent_sdk is claude_adapter.py — and it's reached only via the
factory's lazy import path so 'fake' deployments don't pay the SDK import
cost.
"""

from call_monitor.proposal_assist.drafting.contract import (
    AgentHandle,
    AssistantTextEvent,
    DraftingAgent,
    LaunchContext,
    SDKTurnError,
    StreamEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnDoneEvent,
)
from call_monitor.proposal_assist.drafting.controller import WorkspaceController
from call_monitor.proposal_assist.drafting.factory import build_workspace_controller
from call_monitor.proposal_assist.drafting.locking import LockNotAcquired

__all__ = [
    "AgentHandle",
    "AssistantTextEvent",
    "DraftingAgent",
    "LaunchContext",
    "LockNotAcquired",
    "SDKTurnError",
    "StreamEvent",
    "ToolResultEvent",
    "ToolUseEvent",
    "TurnDoneEvent",
    "WorkspaceController",
    "build_workspace_controller",
]
