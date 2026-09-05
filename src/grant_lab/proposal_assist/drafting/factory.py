"""Build a WorkspaceController for the running environment.

The choice between FakeAgentAdapter and ClaudeAgentSDKAdapter is made
via the DRAFTING_ADAPTER setting. Defaults to 'claude'; tests / demos
set 'fake' to avoid hitting the real API."""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from grant_lab.config import Settings
from grant_lab.db.session import make_drafting_engine, make_session_factory
from grant_lab.proposal_assist.citations import build_citation_service
from grant_lab.proposal_assist.drafting.contract import DraftingAgent
from grant_lab.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry
from grant_lab.proposal_assist.lifecycle import LIFECYCLE
from grant_lab.proposal_assist.port_impl import build_ports


def build_workspace_controller(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    reference_ingest_limiter: object | None = None,
    lock_session_factory: sessionmaker | None = None,
) -> WorkspaceController:
    """Build the WorkspaceController for the running environment.

    Defect 1: drafting advisory-lock sessions are long-lived (held for each
    subprocess's whole life). To keep them off the shared request pool we build
    a DEDICATED engine sized to the live-subprocess ceiling and pass its factory
    as lock_session_factory. Callers may inject lock_session_factory (tests)
    to override; otherwise it's created here. The registry's max_live ceiling
    and this dedicated pool are sized together from the same settings.
    """
    adapter = _build_adapter(settings, session_factory, reference_ingest_limiter)
    registry = SubprocessRegistry(
        adapter=adapter,
        idle_seconds=settings.workspace_idle_timeout_seconds,
        max_live=settings.drafting_max_live_subprocesses,
        lifecycle=LIFECYCLE,
    )
    if lock_session_factory is None:
        drafting_engine = make_drafting_engine(
            settings.database_url,
            pool_size=settings.drafting_db_pool_size,
        )
        lock_session_factory = make_session_factory(drafting_engine)
    return WorkspaceController(
        registry=registry,
        session_factory=session_factory,
        workspaces_dir=settings.workspaces_dir,
        lock_session_factory=lock_session_factory,
    )


def _build_adapter(
    settings: Settings,
    session_factory: sessionmaker,
    reference_ingest_limiter: object | None = None,
) -> DraftingAgent:
    choice = settings.drafting_adapter.lower()
    if choice == "fake":
        return FakeAgentAdapter()
    if choice == "claude":
        # Lazy-import the claude adapter so 'fake' deployments don't pay
        # the SDK import cost.
        from grant_lab.proposal_assist.drafting.claude_adapter import (
            ClaudeAgentSDKAdapter,
        )
        from grant_lab.proposal_assist.tools.registry import (
            ToolContext,
            build_proposal_assist_mcp_server,
        )

        # Build once; shared across all turns in this process. The service
        # holds no open connections (LLM client is lazy/per-call). This is a
        # SEPARATE instance from app.state.citation_service (the web layer's):
        # the drafting agent runs its own tool path. Both share the same
        # session_factory + settings, so caps/ledger are enforced consistently
        # against the same DB rows — do not "deduplicate" them into one.
        _citation_service = build_citation_service(
            settings=settings, session_factory=session_factory
        )

        tool_context_sink: dict = {}

        def tools_factory(repo_dir, workspace_id):
            ctx = ToolContext(
                workspace_id=workspace_id,
                repo_dir=repo_dir,
                session_factory=session_factory,
                settings=settings,
                citation_service=_citation_service,
                reference_ingest_limiter=reference_ingest_limiter,
                ports=build_ports(session_factory),
            )
            # SP2a-2: retain the ctx so set_turn_provenance can stamp the SAME
            # object the MCP tool closures captured (one ctx per workspace launch).
            tool_context_sink[workspace_id] = ctx
            return build_proposal_assist_mcp_server(ctx)

        return ClaudeAgentSDKAdapter(
            model=settings.proposal_assist_llm_model,
            workspaces_dir=settings.workspaces_dir,
            tools_factory=tools_factory,
            tool_context_sink=tool_context_sink,
            # Defect 2: a .env-only ANTHROPIC_API_KEY never reaches os.environ,
            # so thread it explicitly into the subprocess env via the adapter.
            anthropic_api_key=settings.anthropic_api_key,
        )
    raise ValueError(f"unknown DRAFTING_ADAPTER choice: {choice!r} (expected 'claude' or 'fake')")
