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

    return ClaudeAgentSDKAdapter(
        model=settings.proposal_assist_llm_model,
        workspaces_dir=settings.workspaces_dir,
        tools_factory=tools_factory,
        allowed_tools=uncited_claims_allowed_tools(),
    )
