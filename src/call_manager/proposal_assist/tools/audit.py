"""Citation audit MCP tools (Slice 3 Task 5).

`validate_citations` wires the two audit layers from
``citations/audit.py`` behind a single read-only MCP tool:

- the DETERMINISTIC layer (``validate_citations_deterministic``) always runs —
  pure DB/string re-checks of the canonical ledger against live proposal.md +
  chunks; offline-safe.
- the SEMANTIC layer (``validate_citations_semantic``) runs ONLY when an audit
  LLM (``ctx.citation_service``) is available; it LLM-judges whether each live
  citation's evidence actually backs the claim. When the service is absent (test
  / fake path) or capped, the tool degrades to deterministic-only and signals
  this with ``semantic_available: false``.

Read-only: like ``list_citations`` it holds NO workspace lock. The only writes
are verdict-cache INSERTs, which ``store_verdict`` performs on its OWN
short-lived session (see ``verdict_cache``), so the audit's read session is not
entangled with any write transaction.

The impl never raises — the engine functions are contracted not to, and the
registry's ``_safe_tool`` wrapper is the backstop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from call_manager.proposal_assist.citations.audit import (
    validate_citations_deterministic,
    validate_citations_semantic,
)
from call_manager.proposal_assist.tools.citations import _read_proposal_and_ledger

if TYPE_CHECKING:
    from call_manager.proposal_assist.tools.registry import ToolContext


async def validate_citations_impl(ctx: ToolContext) -> dict[str, Any]:
    """Validate every citation: deterministic checks + (when available) a
    semantic LLM judge. Returns ``{"issues", "semantic_available", "checked"}``
    or an ``{"error": ...}`` dict if proposal.md is missing.
    """
    read = _read_proposal_and_ledger(ctx)
    if isinstance(read, dict):
        return read
    proposal_text, ledger = read

    semantic_available = ctx.citation_service is not None

    with ctx.session_factory() as session:
        det = validate_citations_deterministic(
            proposal_text=proposal_text,
            ledger=ledger,
            session=session,
            workspace_id=ctx.workspace_id,
        )
        if semantic_available:
            sem = validate_citations_semantic(
                proposal_text=proposal_text,
                ledger=ledger,
                session=session,
                session_factory=ctx.session_factory,
                workspace_id=ctx.workspace_id,
                citation_service=ctx.citation_service,
                deterministic_issues=det,
            )
        else:
            sem = []

    checked = len([r for r in (ledger.get("evidence") or []) if r.get("id")])

    return {
        "issues": det + sem,
        "semantic_available": semantic_available,
        "checked": checked,
    }
