"""Build a WorkspaceController for the running environment.

The choice between FakeAgentAdapter and ClaudeAgentSDKAdapter is made
via the DRAFTING_ADAPTER setting. Defaults to 'claude'; tests / demos
set 'fake' to avoid hitting the real API."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from call_monitor.config import Settings
from call_monitor.proposal_assist.drafting.contract import DraftingAgent
from call_monitor.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from call_monitor.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_monitor.proposal_assist.drafting.registry import SubprocessRegistry


def build_workspace_controller(
    *,
    settings: Settings,
    session_factory: sessionmaker,
) -> WorkspaceController:
    adapter = _build_adapter(settings)
    registry = SubprocessRegistry(
        adapter=adapter,
        idle_seconds=settings.workspace_idle_timeout_seconds,
    )
    return WorkspaceController(
        registry=registry,
        session_factory=session_factory,
    )


def _build_adapter(settings: Settings) -> DraftingAgent:
    choice = settings.drafting_adapter.lower()
    if choice == "fake":
        return FakeAgentAdapter()
    if choice == "claude":
        # Lazy-import the claude adapter so 'fake' deployments don't pay
        # the SDK import cost.
        from call_monitor.proposal_assist.drafting.claude_adapter import (
            ClaudeAgentSDKAdapter,
        )

        return ClaudeAgentSDKAdapter(
            model=settings.proposal_assist_llm_model,
            workspaces_dir=settings.workspaces_dir,
        )
    raise ValueError(f"unknown DRAFTING_ADAPTER choice: {choice!r} (expected 'claude' or 'fake')")
