"""submit_uncited_claims tool impl: the uncited-claims pass's ONLY tool. Validates the
model's flagged claims against the PINNED draft section count (ctx.uncited_draft_section_count)
and writes the RAW validated list onto the running uncited_claims_jobs row. The verbatim-
substring filter, hashing, and dedupe happen in the pass runner (which holds the pinned
section texts). Mirrors the propose_*/submit_scorecard contract: returns {"error": ...} for
every expected failure, never raises."""

from __future__ import annotations

from typing import Any

from grant_lab.proposal_assist.ports.effects import SubmitUncitedClaims
from grant_lab.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    EffectRejected,
    WorkspaceGenerationStale,
)


def _is_int(x: Any) -> bool:
    # bool is an int SUBCLASS — reject True/False so they cannot pass as a section_index.
    return isinstance(x, int) and not isinstance(x, bool)


async def submit_uncited_claims_impl(
    ctx, *, ports, invocation, claims: list[dict[str, Any]]
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

    # The row lock, the ownership guard and the exactly-once rule moved into
    # port_impl/effects.py::SqlEffects.submit_uncited_claims, with the same strings.
    try:
        ports.effects.submit_uncited_claims(
            invocation.stamp(), SubmitUncitedClaims(claims=out)
        )
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        return {"error": BUSY_ERROR_MESSAGE}
    return {"stored": True, "count": len(out)}
