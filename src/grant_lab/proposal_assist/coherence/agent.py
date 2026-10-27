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

from grant_lab.config import Settings
from grant_lab.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from grant_lab.proposal_assist.drafting.factory import build_sdk_adapter
from grant_lab.proposal_assist.port_impl import build_ports
from grant_lab.proposal_assist.tools.registry import (
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
    lifecycle_generation: int,
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
        allowed_tools=keeper_allowed_tools(locked=locked),
        # M-PA-2: the background-pass bound, not the interactive one.
        max_turns=settings.proposal_assist_max_turns_pass,
    )
