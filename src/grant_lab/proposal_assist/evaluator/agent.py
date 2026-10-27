"""Build the evaluator's OWN one-shot SDK adapter — never WorkspaceController.

The evaluator gets a dedicated adapter + MCP server + ToolContext stamped with
evaluator provenance (origin_kind='evaluator_session', origin_job_id) and the
PINNED section counts (so submit_scorecard validates deep_link against the same
HEAD the pass evaluated). Its ONLY tool is submit_scorecard — no live reads, so
it cannot judge against bytes newer than the recorded evaluated_*_sha.
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


def evaluator_allowed_tools() -> list[str]:
    return ["submit_scorecard"]


def build_evaluator_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    requested_by: int | None,
    job_id: uuid.UUID,
    draft_section_count: int,
    spec_section_count: int,
    rubric_criteria: list[dict],
    lifecycle_generation: int,
    _sdk_factory=ClaudeAgentSDKAdapter,
) -> ClaudeAgentSDKAdapter:
    def tools_factory(repo_dir: Path, ws_id: uuid.UUID):
        ctx = ToolContext(
            workspace_id=ws_id,
            repo_dir=repo_dir,
            session_factory=session_factory,
            settings=settings,
            origin_kind="evaluator_session",
            origin_job_id=job_id,
            created_by=requested_by,
            eval_draft_section_count=draft_section_count,
            eval_spec_section_count=spec_section_count,
            eval_rubric_criteria=rubric_criteria,
            lifecycle_generation=lifecycle_generation,
            ports=build_ports(session_factory),
        )
        return build_proposal_assist_mcp_server(ctx)

    # H4: the org API key and the app-owned CLAUDE_CONFIG_DIR come from the ONE
    # shared constructor every SDK role goes through (drafting/factory.py). It
    # also supplies model + workspaces_dir from the same settings.
    return build_sdk_adapter(
        settings,
        sdk_factory=_sdk_factory,
        tools_factory=tools_factory,
        allowed_tools=evaluator_allowed_tools(),
    )
