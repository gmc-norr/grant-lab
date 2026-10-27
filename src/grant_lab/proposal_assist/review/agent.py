"""Build a mock-panel sub-call's OWN one-shot SDK adapter. Each sub-call (fit-check,
a persona, consolidation) gets a dedicated adapter + MCP server + ToolContext stamped
with review provenance (origin_kind='review_session', origin_job_id=<review_run.id>,
review_run_item_id=<item.id>). Its ONLY tool is submit_review — no live reads, so it
can only judge the pinned instruction. The persona role lives in the INSTRUCTION, not
a system prompt (the adapter has no such parameter)."""

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


def review_allowed_tools() -> list[str]:
    return ["submit_review"]


def build_review_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    review_run_id: uuid.UUID,
    item_id: uuid.UUID,
    kind: str,
    lifecycle_generation: int,
) -> ClaudeAgentSDKAdapter:
    def tools_factory(repo_dir: Path, ws_id: uuid.UUID):
        ctx = ToolContext(
            workspace_id=ws_id,
            repo_dir=repo_dir,
            session_factory=session_factory,
            settings=settings,
            origin_kind="review_session",
            origin_job_id=review_run_id,
            review_run_item_id=item_id,
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
        allowed_tools=review_allowed_tools(),
        # M-PA-2: the background-pass bound, not the interactive one.
        max_turns=settings.proposal_assist_max_turns_pass,
    )
