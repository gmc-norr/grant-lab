"""citation_llm_usage ledger writes + daily-cap checks (internal to the
citation subsystem; call via CitationLLMService). Mirrors turns/caps.py's
Stockholm-day window but a SEPARATE ledger so audit/extraction spend never
silently rides on the drafting turn budget."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from grant_lab.config import Settings
from grant_lab.db.models import CitationLlmUsage
from grant_lab.proposal_assist.spend import record_spend
from grant_lab.timeutil import stockholm_day_start_utc


@dataclass(frozen=True)
class CapStatus:
    exceeded: bool
    scope: str | None  # "workspace" | "global" | None


# CITATION_LLM_PROVIDER values that run ON-PREM. Such a call spends GPU time on
# the 2080 Ti, not Anthropic money, so its tokens must not consume
# `check_global_daily_token_cap` -- that cap's whole subject is the hosted bill,
# and with the default provider (`ollama`) EVERY citation row was feeding it,
# able to deny an Opus drafting turn (review 2026-10-26). The citation ledger's
# OWN per-op caps still see these rows: an on-prem volume ceiling belongs there.
#
# A name not listed here counts as hosted, so an unrecognised provider
# over-counts rather than silently stopping the bill. `build_citation_llm`
# refuses any provider but `ollama` and `anthropic`, so this is defence in depth.
_LOCAL_PROVIDERS = frozenset({"ollama"})





def record_usage(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    op: str,
    status: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    model: str | None = None,
    provider: str,
) -> None:
    """Append one usage row. Caller commits.

    `provider` is `CITATION_LLM_PROVIDER` and has no default on purpose: it
    decides whether this row is hosted money, and a caller that forgets it would
    silently bill the Anthropic cap for on-prem GPU time (or the reverse). It is
    NOT stored on `citation_llm_usage` -- that ledger's own caps apply to both
    providers -- only on the `llm_spend` row, which is a hosted-bill record.
    """
    session.add(
        CitationLlmUsage(
            workspace_id=workspace_id,
            op=op,
            status=status,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
        )
    )
    # Also the global ledger. `citation_llm_usage` keeps its own per-op caps —
    # this is what makes citation spend count toward the GLOBAL daily cap, which
    # never summed it and was therefore not global at all. One writer, so the two
    # cannot drift.
    record_spend(
        session,
        workspace_id=workspace_id,
        kind="citation",
        status=status,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        model=model,
        ref_id=op,
        # The provider tag. The raw `model` cannot carry it: measured, it is
        # NULL on the failed and timeout paths for BOTH providers, and one
        # CITATION_LLM_MODEL feeds both branches of `build_citation_llm`, so
        # even the ok path can collide. See spend.LOCAL_MODEL_PREFIX.
        local=provider.lower() in _LOCAL_PROVIDERS,
    )


def _sum_today(
    session: Session, *, workspace_id: uuid.UUID | None, settings: Settings
) -> tuple[int, int]:
    """Today's (tokens_in, tokens_out), with every `timeout` row charged as a
    reply of `citation_llm_max_output_tokens` output tokens.

    A `timeout` is written only for a read timeout on a SENT request
    (AnthropicTimeoutError; Ollama timeouts are plain LLMErrors and land as
    `failed`). The server may have finished and billed that reply, but the row
    carries 0 tokens because the client never saw a count, so a token-only sum
    let timeouts repeat forever under the cap (review 2026-10-06 M7). The
    client's own max_tokens is the most such a reply can have cost in output.
    The charge is applied HERE, at read time, rather than written into the
    ledger, so the ledger keeps recording only what the API reported."""
    stmt = select(
        func.coalesce(func.sum(CitationLlmUsage.tokens_in), 0),
        func.coalesce(func.sum(CitationLlmUsage.tokens_out), 0),
        func.count().filter(CitationLlmUsage.status == "timeout"),
    ).where(CitationLlmUsage.created_at >= stockholm_day_start_utc())
    if workspace_id is not None:
        stmt = stmt.where(CitationLlmUsage.workspace_id == workspace_id)
    row = session.execute(stmt).one()
    timeouts = int(row[2])
    return int(row[0]), int(row[1]) + timeouts * settings.citation_llm_max_output_tokens


def check_citation_caps(
    session: Session, workspace_id: uuid.UUID, *, settings: Settings
) -> CapStatus:
    """Read-only; `>=` hard ceiling (matches turns/caps.py). Workspace first."""
    # Soft guardrail, not an exact ledger: this reads usage then the caller
    # makes the call + records in separate sessions, so two concurrent calls
    # near the ceiling can both pass and overshoot by one. Matches the
    # intentional approximate-cap semantics in turns/caps.py.
    ws_in, ws_out = _sum_today(session, workspace_id=workspace_id, settings=settings)
    if (
        ws_in >= settings.citation_llm_daily_token_cap_input
        or ws_out >= settings.citation_llm_daily_token_cap_output
    ):
        return CapStatus(True, "workspace")
    g_in, g_out = _sum_today(session, workspace_id=None, settings=settings)
    if (
        g_in >= settings.global_citation_llm_daily_token_cap_input
        or g_out >= settings.global_citation_llm_daily_token_cap_output
    ):
        return CapStatus(True, "global")
    return CapStatus(False, None)
