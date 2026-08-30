"""citation_llm_usage ledger writes + daily-cap checks (internal to the
citation subsystem; call via CitationLLMService). Mirrors turns/caps.py's
Stockholm-day window but a SEPARATE ledger so audit/extraction spend never
silently rides on the drafting turn budget."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import CitationLlmUsage
from call_manager.proposal_assist.spend import record_spend
from call_manager.timeutil import stockholm_day_start_utc


@dataclass(frozen=True)
class CapStatus:
    exceeded: bool
    scope: str | None  # "workspace" | "global" | None





def record_usage(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    op: str,
    status: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    model: str | None = None,
) -> None:
    """Append one usage row. Caller commits."""
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
    )


def _sum_today(session: Session, *, workspace_id: uuid.UUID | None) -> tuple[int, int]:
    stmt = select(
        func.coalesce(func.sum(CitationLlmUsage.tokens_in), 0),
        func.coalesce(func.sum(CitationLlmUsage.tokens_out), 0),
    ).where(CitationLlmUsage.created_at >= stockholm_day_start_utc())
    if workspace_id is not None:
        stmt = stmt.where(CitationLlmUsage.workspace_id == workspace_id)
    row = session.execute(stmt).one()
    return int(row[0]), int(row[1])


def check_citation_caps(
    session: Session, workspace_id: uuid.UUID, *, settings: Settings
) -> CapStatus:
    """Read-only; `>=` hard ceiling (matches turns/caps.py). Workspace first."""
    # Soft guardrail, not an exact ledger: this reads usage then the caller
    # makes the call + records in separate sessions, so two concurrent calls
    # near the ceiling can both pass and overshoot by one. Matches the
    # intentional approximate-cap semantics in turns/caps.py.
    ws_in, ws_out = _sum_today(session, workspace_id=workspace_id)
    if (
        ws_in >= settings.citation_llm_daily_token_cap_input
        or ws_out >= settings.citation_llm_daily_token_cap_output
    ):
        return CapStatus(True, "workspace")
    g_in, g_out = _sum_today(session, workspace_id=None)
    if (
        g_in >= settings.global_citation_llm_daily_token_cap_input
        or g_out >= settings.global_citation_llm_daily_token_cap_output
    ):
        return CapStatus(True, "global")
    return CapStatus(False, None)
