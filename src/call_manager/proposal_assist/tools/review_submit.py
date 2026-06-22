"""submit_review tool impl: the ONLY tool a mock-panel sub-call (fit-check, a
persona, or consolidation) may call. Validates a non-empty markdown body and
writes it onto the running ReviewRunItem identified by ctx.review_run_item_id.
Mirrors the propose_*/submit_scorecard contract: returns {"error": ...} for every
expected failure, never raises. Exactly-once: a second call errors."""

from __future__ import annotations

from typing import Any

from call_manager.db.models import ReviewRun, ReviewRunItem
from call_manager.proposal_assist.tools.registry import ToolContext


async def submit_review_impl(ctx: ToolContext, *, markdown: str) -> dict[str, Any]:
    body = markdown.strip() if isinstance(markdown, str) else ""
    if not body:
        return {"error": "markdown must be a non-empty string"}
    with ctx.session_factory() as session:
        item = session.get(ReviewRunItem, ctx.review_run_item_id, with_for_update=True)
        if (
            item is None
            or item.review_run_id != ctx.origin_job_id
            or item.status != "running"
        ):
            return {"error": "submit_review target is not a running item for this run"}
        # Verify workspace ownership of the parent run (mirrors submit_scorecard's
        # job.workspace_id == ctx.workspace_id guard) — a stale/mismatched ctx must
        # never write into another workspace's run.
        run = session.get(ReviewRun, item.review_run_id)
        if run is None or run.workspace_id != ctx.workspace_id or run.status != "running":
            return {"error": "submit_review target run is not active for this workspace"}
        if item.raw_response_json is not None:
            return {"error": "a review has already been submitted for this item"}
        item.raw_response_json = {"markdown": body}
        session.commit()
    return {"stored": True}
