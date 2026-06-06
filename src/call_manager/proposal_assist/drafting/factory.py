"""Build a WorkspaceController for the running environment.

The choice between FakeAgentAdapter and ClaudeAgentSDKAdapter is made
via the DRAFTING_ADAPTER setting. Defaults to 'claude'; tests / demos
set 'fake' to avoid hitting the real API."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.proposal_assist.citations import build_citation_service
from call_manager.proposal_assist.drafting.contract import DraftingAgent
from call_manager.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry


def build_workspace_controller(
    *,
    settings: Settings,
    session_factory: sessionmaker,
) -> WorkspaceController:
    adapter = _build_adapter(settings, session_factory)
    registry = SubprocessRegistry(
        adapter=adapter,
        idle_seconds=settings.workspace_idle_timeout_seconds,
    )
    return WorkspaceController(
        registry=registry,
        session_factory=session_factory,
        workspaces_dir=settings.workspaces_dir,
    )


def _build_adapter(settings: Settings, session_factory: sessionmaker) -> DraftingAgent:
    choice = settings.drafting_adapter.lower()
    if choice == "fake":
        return FakeAgentAdapter()
    if choice == "claude":
        # Lazy-import the claude adapter so 'fake' deployments don't pay
        # the SDK import cost.
        from call_manager.proposal_assist.drafting.claude_adapter import (
            ClaudeAgentSDKAdapter,
        )
        from call_manager.proposal_assist.tools.registry import (
            ToolContext,
            build_proposal_assist_mcp_server,
        )

        # Build once; shared across all turns in this process. The service
        # holds no open connections (LLM client is lazy/per-call).
        _citation_service = build_citation_service(
            settings=settings, session_factory=session_factory
        )

        def tools_factory(repo_dir, workspace_id):
            ctx = ToolContext(
                workspace_id=workspace_id,
                repo_dir=repo_dir,
                session_factory=session_factory,
                settings=settings,
                citation_service=_citation_service,
            )
            return build_proposal_assist_mcp_server(ctx)

        return ClaudeAgentSDKAdapter(
            model=settings.proposal_assist_llm_model,
            workspaces_dir=settings.workspaces_dir,
            tools_factory=tools_factory,
        )
    raise ValueError(f"unknown DRAFTING_ADAPTER choice: {choice!r} (expected 'claude' or 'fake')")
