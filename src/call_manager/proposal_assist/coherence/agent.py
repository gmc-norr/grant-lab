"""Build the coherence-keeper's OWN one-shot SDK adapter — never WorkspaceController.

The keeper gets a dedicated adapter + MCP server + ToolContext stamped with
coherence provenance at factory time (origin_kind/origin_job_id), launched with no
resume, terminated by the pass runner in a finally. This keeps it isolated from the
interactive session (no shared subprocess, no last_sdk_session_id, no chat thread).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from call_manager.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)

# Lock-aware: when the spec is locked, propose_spec_edit is removed so a spec edit
# is structurally unreachable. read_* are required to set the anti-clobber snapshot.
_BASE = ["read_proposal", "read_spec", "propose_edit"]


def keeper_allowed_tools(*, locked: bool) -> list[str]:
    return list(_BASE) if locked else [*_BASE, "propose_spec_edit"]


def build_keeper_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    requested_by: int | None,
    job_id: uuid.UUID,
    locked: bool,
) -> ClaudeAgentSDKAdapter:
    def tools_factory(repo_dir: Path, ws_id: uuid.UUID):
        ctx = ToolContext(
            workspace_id=ws_id,
            repo_dir=repo_dir,
            session_factory=session_factory,
            settings=settings,
            origin_kind="coherence_session",
            origin_job_id=job_id,
            created_by=requested_by,
        )
        return build_proposal_assist_mcp_server(ctx)

    return ClaudeAgentSDKAdapter(
        model=settings.proposal_assist_llm_model,
        workspaces_dir=settings.workspaces_dir,
        tools_factory=tools_factory,
        allowed_tools=keeper_allowed_tools(locked=locked),
    )
