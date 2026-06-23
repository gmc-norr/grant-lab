"""submit_uncited_claims tool impl: the uncited-claims pass's ONLY tool. Validates the
model's flagged claims against the PINNED draft section count (ctx.uncited_draft_section_count)
and writes the RAW validated list onto the running uncited_claims_jobs row. The verbatim-
substring filter, hashing, and dedupe happen in the pass runner (which holds the pinned
section texts). Mirrors the propose_*/submit_scorecard contract: returns {"error": ...} for
every expected failure, never raises."""

from __future__ import annotations

from typing import Any

from call_manager.db.models import UncitedClaimsJob
from call_manager.proposal_assist.tools.registry import ToolContext


def _is_int(x: Any) -> bool:
    # bool is an int SUBCLASS — reject True/False so they cannot pass as a section_index.
    return isinstance(x, int) and not isinstance(x, bool)


async def submit_uncited_claims_impl(
    ctx: ToolContext, *, claims: list[dict[str, Any]]
) -> dict[str, Any]:
    n = ctx.uncited_draft_section_count or 0
    out: list[dict[str, Any]] = []
    for c in claims:
        raw_text = c.get("claim_text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not text:
            return {"error": "each claim needs a non-empty claim_text"}
        si = c.get("section_index")
        if not _is_int(si) or not (0 <= si < n):
            return {"error": f"claim section_index must be an int in [0,{n}); got {si!r}"}
        raw_reason = c.get("reason")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        if not reason:
            return {"error": "each claim needs a non-empty reason"}
        out.append({"claim_text": text, "section_index": si, "reason": reason})

    # Guarded write: row-lock the target; require it is THIS workspace's still-running job
    # (rejects a stale ctx or a job the reaper/cancel already failed). Exactly-once: a second
    # call must not overwrite the first.
    with ctx.session_factory() as session:
        job = session.get(UncitedClaimsJob, ctx.origin_job_id, with_for_update=True)
        if job is None or job.workspace_id != ctx.workspace_id or job.status != "running":
            return {"error": "target is not an active uncited-claims run for this workspace"}
        if job.claims is not None:
            return {"error": "claims have already been submitted for this run"}
        job.claims = out
        session.commit()
    return {"stored": True, "count": len(out)}
