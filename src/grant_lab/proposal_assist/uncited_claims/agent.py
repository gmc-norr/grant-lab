"""Build the uncited-claims pass's OWN one-shot SDK adapter — never WorkspaceController.

A dedicated adapter + MCP server + ToolContext stamped with uncited-claims provenance
(origin_kind='uncited_claims_session', origin_job_id) and the PINNED draft section count
(so submit_uncited_claims validates section_index against the same draft scanned). Its ONLY
tool is submit_uncited_claims."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from grant_lab.config import Settings
from grant_lab.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from grant_lab.proposal_assist.drafting.factory import build_sdk_adapter
from grant_lab.proposal_assist.port_impl import build_ports
from grant_lab.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)


def uncited_claims_allowed_tools() -> list[str]:
    return ["submit_uncited_claims"]


def build_uncited_claims_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    requested_by: int | None,
    job_id: uuid.UUID,
    draft_section_count: int,
    lifecycle_generation: int,
) -> ClaudeAgentSDKAdapter:
    def tools_factory(repo_dir: Path, ws_id: uuid.UUID):
        ctx = ToolContext(
            workspace_id=ws_id,
            repo_dir=repo_dir,
            session_factory=session_factory,
            settings=settings,
            origin_kind="uncited_claims_session",
            origin_job_id=job_id,
            created_by=requested_by,
            uncited_draft_section_count=draft_section_count,
            lifecycle_generation=lifecycle_generation,
            ports=build_ports(session_factory),
        )
        return build_proposal_assist_mcp_server(ctx)

    # H4: the org API key and the app-owned CLAUDE_CONFIG_DIR come from the ONE
    # shared constructor every SDK role goes through (drafting/factory.py). It
    # also supplies model + workspaces_dir from the same settings.
    return build_sdk_adapter(
        settings,
        sdk_factory=ClaudeAgentSDKAdapter,
        tools_factory=tools_factory,
        allowed_tools=uncited_claims_allowed_tools(),
        # M-PA-2: the background-pass bound, not the interactive one.
        max_turns=settings.proposal_assist_max_turns_pass,
    )
