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

from call_manager.config import Settings
from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from call_manager.proposal_assist.tools.registry import (
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
        )
        return build_proposal_assist_mcp_server(ctx)

    return ClaudeAgentSDKAdapter(
        model=settings.proposal_assist_llm_model,
        workspaces_dir=settings.workspaces_dir,
        tools_factory=tools_factory,
        allowed_tools=review_allowed_tools(),
    )
