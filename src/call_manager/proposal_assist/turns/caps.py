"""Per-workspace daily-token-cap enforcement.

Stockholm-day boundary: tokens count toward "today" if their chat_turn's
created_at falls within the current Europe/Stockholm day. Cap defaults
live on Settings; per-workspace overrides live on
workspaces.daily_token_cap_{in,out}_override.

This module is enforcement-only. B8 owns the user-facing "you've hit the
cap" UX (banner, admin override widget, "paused until midnight" status).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import (
    ChatTurn,
    CoherenceJob,
    EvaluatorJob,
    ReviewRun,
    UncitedClaimsJob,
    Workspace,
)
from call_manager.timeutil import stockholm_day_start_utc


@dataclass
class CapCheckResult:
    allowed: bool
    used_in: int
    used_out: int
    cap_in: int
    cap_out: int
    reason: str | None = None  # 'input_cap_exceeded' | 'output_cap_exceeded' | None


class TokenCapExceeded(RuntimeError):
    def __init__(
        self,
        workspace_id: uuid.UUID,
        *,
        used_in: int,
        used_out: int,
        cap_in: int,
        cap_out: int,
        reason: str,
    ) -> None:
        super().__init__(
            f"workspace {workspace_id} {reason}: "
            f"used_in={used_in} used_out={used_out} cap_in={cap_in} cap_out={cap_out}"
        )
        self.workspace_id = workspace_id
        self.used_in = used_in
        self.used_out = used_out
        self.cap_in = cap_in
        self.cap_out = cap_out
        self.reason = reason


class GlobalTokenCapExceeded(RuntimeError):
    """Raised when the org-wide daily-token ceiling is reached.

    Distinct from TokenCapExceeded so the web layer can differentiate
    'your workspace ran out' vs 'the whole org ran out'."""

    def __init__(
        self,
        *,
        used_in: int,
        used_out: int,
        cap_in: int,
        cap_out: int,
        reason: str,
    ) -> None:
        super().__init__(
            f"global {reason}: used_in={used_in} used_out={used_out} "
            f"cap_in={cap_in} cap_out={cap_out}"
        )
        self.used_in = used_in
        self.used_out = used_out
        self.cap_in = cap_in
        self.cap_out = cap_out
        self.reason = reason





def check_daily_token_cap(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    settings: Settings,
) -> CapCheckResult:
    """Sum tokens used today against the effective cap. Read-only — does
    not raise; caller (submit_user_turn) decides how to react."""
    day_start_utc = stockholm_day_start_utc()

    sums = session.execute(
        select(
            func.coalesce(func.sum(ChatTurn.tokens_in), 0),
            func.coalesce(func.sum(ChatTurn.tokens_out), 0),
        ).where(
            ChatTurn.workspace_id == workspace_id,
            ChatTurn.created_at >= day_start_utc,
        )
    ).one()
    used_in, used_out = int(sums[0]), int(sums[1])

    ws = session.scalar(select(Workspace).where(Workspace.id == workspace_id))
    cap_in = (
        ws.daily_token_cap_in_override
        if ws is not None and ws.daily_token_cap_in_override is not None
        else settings.daily_token_cap_input
    )
    cap_out = (
        ws.daily_token_cap_out_override
        if ws is not None and ws.daily_token_cap_out_override is not None
        else settings.daily_token_cap_output
    )

    # Reject AT-OR-OVER the cap (`>=`, not `>`). The spec says "exceeding"
    # the cap returns a chat error, but reading it strictly (`>`) would let
    # a single response push us tens of thousands of tokens past the budget
    # before the NEXT turn finally trips the check — the cap stops being
    # protective. `>=` makes the cap a hard ceiling: once we hit it, no
    # more turns can start (the in-flight one runs to completion, see
    # plan self-review note 5). Override=0 leverages this as a workspace
    # pause toggle: an admin setting daily_token_cap_in_override=0 blocks
    # all new turns until they raise it again.
    reason: str | None = None
    if used_in >= cap_in:
        reason = "input_cap_exceeded"
    elif used_out >= cap_out:
        reason = "output_cap_exceeded"

    return CapCheckResult(
        allowed=reason is None,
        used_in=used_in,
        used_out=used_out,
        cap_in=cap_in,
        cap_out=cap_out,
        reason=reason,
    )


def check_global_daily_token_cap(
    session: Session,
    *,
    settings: Settings,
) -> CapCheckResult:
    """Sum today's input/output tokens across ALL workspaces against the
    global ceiling. Read-only; caller (submit_user_turn) raises.

    Coverage (B9): this org-wide backstop UNIONs ALL FIVE cloud-spend tables,
    not just chat turns. Each of the four background queues — coherence,
    evaluator, mock-panel review, uncited-claims — tracks its tokens on its OWN
    table (the member-triggerable fan-outs in web/coherence.py, web/evaluator.py,
    web/review.py, web/uncited_claims.py), so summing only ChatTurn left the bulk
    of cloud consumers invisible to the global guardrail. We coalesce-sum each
    table's today rows and add the five totals:
      - ChatTurn.tokens_{in,out}              (created_at  >= day start)
      - CoherenceJob.{input,output}_tokens    (queued_at   >= day start)
      - EvaluatorJob.{input,output}_tokens    (queued_at   >= day start)
      - ReviewRun.{input,output}_tokens       (triggered_at >= day start;
        run-level totals already roll up per-item spend, incl. reaped runs)
      - UncitedClaimsJob.{input,output}_tokens (queued_at  >= day start)
    Each table is queried separately (no SQL JOIN) so unrelated row counts can't
    fan out and multiply the sums.

    Concurrency: NO global advisory lock. The cap is a guardrail
    against runaway org-wide spend, not an exact ledger. Two concurrent
    submits in different workspaces can both pass a near-cap global
    check and queue, overshooting by one turn each. Acceptable because
    a global lock would bottleneck all submits org-wide. If precise
    enforcement becomes a requirement, add `pg_try_advisory_xact_lock`
    on a stable per-day key (e.g. hashtext('global-cap:' || YYYYMMDD)).
    """
    day_start_utc = stockholm_day_start_utc()
    chat = session.execute(
        select(
            func.coalesce(func.sum(ChatTurn.tokens_in), 0),
            func.coalesce(func.sum(ChatTurn.tokens_out), 0),
        ).where(ChatTurn.created_at >= day_start_utc)
    ).one()
    coherence = session.execute(
        select(
            func.coalesce(func.sum(CoherenceJob.input_tokens), 0),
            func.coalesce(func.sum(CoherenceJob.output_tokens), 0),
        ).where(CoherenceJob.queued_at >= day_start_utc)
    ).one()
    evaluator = session.execute(
        select(
            func.coalesce(func.sum(EvaluatorJob.input_tokens), 0),
            func.coalesce(func.sum(EvaluatorJob.output_tokens), 0),
        ).where(EvaluatorJob.queued_at >= day_start_utc)
    ).one()
    review = session.execute(
        select(
            func.coalesce(func.sum(ReviewRun.input_tokens), 0),
            func.coalesce(func.sum(ReviewRun.output_tokens), 0),
        ).where(ReviewRun.triggered_at >= day_start_utc)
    ).one()
    uncited = session.execute(
        select(
            func.coalesce(func.sum(UncitedClaimsJob.input_tokens), 0),
            func.coalesce(func.sum(UncitedClaimsJob.output_tokens), 0),
        ).where(UncitedClaimsJob.queued_at >= day_start_utc)
    ).one()
    used_in = (
        int(chat[0]) + int(coherence[0]) + int(evaluator[0]) + int(review[0]) + int(uncited[0])
    )
    used_out = (
        int(chat[1]) + int(coherence[1]) + int(evaluator[1]) + int(review[1]) + int(uncited[1])
    )
    cap_in = settings.global_daily_token_cap_input
    cap_out = settings.global_daily_token_cap_output
    reason: str | None = None
    if used_in >= cap_in:
        reason = "input_cap_exceeded"
    elif used_out >= cap_out:
        reason = "output_cap_exceeded"
    return CapCheckResult(
        allowed=reason is None,
        used_in=used_in,
        used_out=used_out,
        cap_in=cap_in,
        cap_out=cap_out,
        reason=reason,
    )
