"""Per-workspace tool surface for the drafting agent.

The registry pattern (registry.py) is the contract: a ToolContext binds
workspace-specific resources (repo_dir, session_factory, settings) to a
fresh MCP server per workspace launch.

B7 will extend this surface with figure/attachment tools by adding more
@tool functions to build_proposal_assist_mcp_server.
"""

from grant_lab.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)

__all__ = ["ToolContext", "build_proposal_assist_mcp_server"]
