"""submit_review tool impl: the ONLY tool a mock-panel sub-call (fit-check, a
persona, or consolidation) may call. Validates a non-empty markdown body and
hands it to Effects.submit_review, which writes it onto the running ReviewRunItem
identified by ctx.review_run_item_id. Mirrors the propose_*/submit_scorecard
contract: returns {"error": ...} for every expected failure, never raises.
Exactly-once: a second call errors."""

from __future__ import annotations

from typing import Any

from call_manager.proposal_assist.ports.effects import SubmitReview
from call_manager.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    EffectRejected,
    WorkspaceGenerationStale,
)


async def submit_review_impl(ctx, *, ports, invocation, markdown: str) -> dict[str, Any]:
    # The strip-and-reject stays HERE. ports/effects.py:32-36: the port stores
    # cmd.markdown verbatim and does not reject an empty result — that is the tool
    # boundary's job, and a converter that drops this step loses it silently.
    body = markdown.strip() if isinstance(markdown, str) else ""
    if not body:
        return {"error": "markdown must be a non-empty string"}
    try:
        ports.effects.submit_review(
            invocation.stamp(),
            SubmitReview(review_run_item_id=ctx.review_run_item_id, markdown=body),
        )
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        return {"error": BUSY_ERROR_MESSAGE}
    return {"stored": True}
