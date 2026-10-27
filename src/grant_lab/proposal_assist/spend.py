"""The one place hosted-LLM spend is written, and the one place it is read.

Before this, spend was RE-DERIVED at read time by summing five tables on three
different timestamp columns. Every new feature had to remember two columns, a
write on every terminal path, and a SELECT in caps.py — and six sites had got it
wrong, every one of them in the same direction: money spent, nothing recorded.

Rules for callers:
  * Record on EVERY terminal path, including failures and timeouts. A run that
    errored after consuming input tokens cost real money.
  * Record even when the token counts are None. "We spent nothing" and "we never
    looked" are different facts; a missing row cannot express the second.
  * The caller commits. This only adds to the session, so the row normally lands
    in the same transaction as the job's own status change and cannot half-happen.
  * TWO deliberate exceptions: turns/compaction.py commits its success row on
    its own session BEFORE writing the summary, and phases/setup.py commits each
    extractor's row before the locked persist (which a cancel, a delete or a
    re-ingest can roll back). Atomicity is the wrong priority once the money is
    already spent — a failure persisting the result must not roll the row back
    with it. Prefer the same transaction; break it only where the spend would
    otherwise be lost, and say so at the call site.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from grant_lab.config import Settings
from grant_lab.db.models import LlmSpend
from grant_lab.timeutil import stockholm_day_start_utc

SpendKind = Literal[
    "chat_turn",
    "compaction",
    "coherence",
    "evaluator",
    "review",
    "uncited_claims",
    "citation",
    # Phase 0 workspace setup: the section and rubric extractors, one Opus call
    # each per new call (review 2026-10-06 H2 — they billed outside the ledger).
    "setup",
]
SpendStatus = Literal["ok", "failed", "timeout"]

__all__ = [
    "ANTHROPIC_CLIENT_MAX_OUTPUT_TOKENS",
    "LOCAL_MODEL_PREFIX",
    "SpendKind",
    "SpendStatus",
    "count_billed_attempts",
    "record_spend",
    "sum_spend_today",
]

# `ranking.anthropic_client.AnthropicClient.__init__`'s own max_tokens default,
# which scheduler.py's compaction and Phase 0 factories both leave alone. Copied
# rather than imported so this module does not pull the Anthropic SDK in;
# tests/proposal_assist/test_spend.py pins the two equal by signature.
ANTHROPIC_CLIENT_MAX_OUTPUT_TOKENS = 4096

# A row produced by an ON-PREM model, stamped into `model` by `record_spend`'s
# `local=True`. This ledger's readers are HOSTED-spend caps, and the one
# subsystem with a provider choice is citations (CITATION_LLM_PROVIDER, default
# `ollama`): on-prem extraction spends GPU time, not money, and summing it into
# `check_global_daily_token_cap` let a reference-heavy afternoon deny an Opus
# drafting turn (review 2026-10-26).
#
# The tag goes in the EXISTING `model` column, so no migration: the raw value
# cannot carry the distinction on its own -- it is NULL on the `failed` and
# `timeout` paths for either provider, and one CITATION_LLM_MODEL feeds both
# branches of `build_citation_llm`, so even the `ok` path collides. Stamping it
# at write time makes the classification exact instead of a heuristic.
#
# No backfill either: every reader here looks at ONE Stockholm day, so by the
# first midnight after deploy every row in scope was written by this writer.
# Until then, untagged local rows keep counting -- over-counting, which is the
# safe direction for a spend guardrail.
LOCAL_MODEL_PREFIX = "local:"


def _timeout_output_charge(settings: Settings) -> dict[str, int]:
    """What a `timeout` row of each kind can have cost in OUTPUT tokens.

    The M7 rule, from `citations/usage.py::_sum_today`: a `timeout` is a read
    timeout on a request that WAS SENT, so the server may have finished and
    billed the reply, but the row carries 0 tokens because the client never saw
    a count. "The client's own max_tokens is the most such a reply can have cost
    in output", charged at READ time so the ledger keeps recording only what the
    API reported.

    Only kinds whose client carries a max_tokens appear here, and the `citation`
    entry is byte-for-byte the expression `_sum_today` uses, so the two caps
    cannot disagree about the same row. The five Claude Agent SDK kinds
    (chat_turn, coherence, evaluator, review, uncited_claims) are absent on
    purpose: an SDK turn is a multi-call agent session with no max_tokens, so
    there is no ceiling the client enforced and any number here would be
    invented. Give one of them an OUTPUT bound and it belongs in this dict —
    `PROPOSAL_ASSIST_MAX_TURNS_*` (M-PA-2) is deliberately not that: it bounds
    how many agent turns a request may take, not how many output tokens any one
    of them may produce, so it still implies no charge. Those kinds also no
    longer need one as badly: since M-PA-1 a failed or cancelled SDK turn
    records the tally it actually streamed instead of a zero.
    """
    return {
        "citation": settings.citation_llm_max_output_tokens,
        "compaction": ANTHROPIC_CLIENT_MAX_OUTPUT_TOKENS,
        "setup": ANTHROPIC_CLIENT_MAX_OUTPUT_TOKENS,
    }


def record_spend(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    kind: SpendKind,
    status: SpendStatus,
    tokens_in: int | None = 0,
    tokens_out: int | None = 0,
    model: str | None = None,
    ref_id: str | uuid.UUID | int | None = None,
    sdk_session_id: str | None = None,
    local: bool = False,
) -> None:
    """Append one spend row. The CALLER COMMITS.

    `tokens_in`/`tokens_out` accept None and store 0 — the point is that the row
    exists. `ref_id` accepts whatever the owning table keys on (uuid for jobs,
    int for chat turns, None for compaction, which has no row) and is stored as
    text, so one column serves all of them.

    `local=True` marks a row an ON-PREM model produced: the row is still
    recorded (an unrecorded call is the defect this module exists to prevent)
    but `sum_spend_today` leaves it out of the hosted caps. It defaults False
    because every other kind reaches a hosted API by construction — only
    citations have a provider switch. See LOCAL_MODEL_PREFIX.

    A hosted row may NOT carry the prefix: `sum_spend_today` decides the
    exclusion from the column's text alone, so the prefix is this writer's
    stamp, never a model id a caller may supply. See the guard below.
    """
    # The tag is in-band, so the only thing separating a hosted row from an
    # invisible one is that nobody else writes those six characters. A model
    # string reaches here from config (CITATION_LLM_MODEL feeds the hosted
    # branch of `build_citation_llm` unchanged) and from API echoes
    # (compaction's `envelope["model"]`), neither of which this module controls
    # -- so refuse rather than record spend the hosted caps cannot see. Loud:
    # the row is lost, but a row that reads as local IS lost, silently, and for
    # the whole Stockholm day. Raised before the `session.add` so the caller's
    # transaction is untouched. The matching validator in config.py stops the
    # one operator-settable path at BOOT; this guard covers every writer.
    if model and not local and model.startswith(LOCAL_MODEL_PREFIX):
        raise ValueError(
            f"a hosted spend row may not carry the {LOCAL_MODEL_PREFIX!r} tag "
            f"(kind={kind} model={model!r}): that prefix is record_spend's own "
            "stamp for local=True and sum_spend_today EXCLUDES it, so this row "
            "would hide real hosted spend from every Anthropic daily cap. Pass "
            "local=True for an on-prem call, or a model id without the prefix."
        )
    session.add(
        LlmSpend(
            workspace_id=workspace_id,
            kind=kind,
            ref_id=None if ref_id is None else str(ref_id),
            status=status,
            tokens_in=int(tokens_in or 0),
            tokens_out=int(tokens_out or 0),
            model=f"{LOCAL_MODEL_PREFIX}{model or ''}" if local else model,
            sdk_session_id=sdk_session_id,
        )
    )


def sum_spend_today(
    session: Session,
    *,
    settings: Settings,
    workspace_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """`(tokens_in, tokens_out)` since Stockholm midnight, all kinds, with every
    `timeout` row charged at its kind's max output tokens (see
    `_timeout_output_charge`).

    HOSTED rows only: rows stamped `local=True` are excluded, tokens and timeout
    charge alike. Every reader of this function is an Anthropic-bill ceiling.

    `settings` is required rather than defaulted: a timeout charge that silently
    switches off when a caller forgets the argument is the defect this closes,
    not a convenience.

    `now` is a parameter for the same reason `stockholm_day_start_utc` takes one:
    a function that reads the clock itself cannot be falsified by a test.
    """
    charges = _timeout_output_charge(settings)
    stmt = select(
        func.coalesce(func.sum(LlmSpend.tokens_in), 0),
        func.coalesce(func.sum(LlmSpend.tokens_out), 0),
        # One count per chargeable kind, in `charges` order. Same shape as
        # `_sum_today`'s `func.count().filter(status == "timeout")`, split by
        # kind because the ceilings differ.
        *[
            func.count().filter(LlmSpend.status == "timeout", LlmSpend.kind == kind)
            for kind in charges
        ],
    ).where(
        LlmSpend.created_at >= stockholm_day_start_utc(now),
        # NOT LIKE on a NULL model yields NULL, which would drop the row -- and
        # a NULL model is the COMMON case here (every failed/timeout row, and
        # compaction's success rows). The explicit IS NULL arm is load-bearing.
        or_(
            LlmSpend.model.is_(None),
            ~LlmSpend.model.startswith(LOCAL_MODEL_PREFIX),
        ),
    )
    if workspace_id is not None:
        stmt = stmt.where(LlmSpend.workspace_id == workspace_id)
    row = session.execute(stmt).one()
    timeout_out = sum(
        int(count) * charges[kind] for kind, count in zip(charges, row[2:], strict=True)
    )
    return int(row[0]), int(row[1]) + timeout_out


def count_billed_attempts(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    kind: SpendKind,
    since: datetime,
) -> int:
    """Rows of `kind` for this workspace since `since` that may have cost
    money: any recorded tokens, whatever the status, or a `timeout` (the
    server may have finished and billed a reply the client gave up on). A
    failure with no tokens never got an answer and cost nothing. compaction.py
    passes `since` = the later of 24 h ago and the workspace's last summary, so
    an `ok` row after it is a billed call whose summary write failed. It lives
    here because this module is the ledger's only reader and writer
    (tests/fencing/test_spend_guard.py)."""
    return session.scalar(
        select(func.count())
        .select_from(LlmSpend)
        .where(
            LlmSpend.workspace_id == workspace_id,
            LlmSpend.kind == kind,
            or_(LlmSpend.tokens_in + LlmSpend.tokens_out > 0, LlmSpend.status == "timeout"),
            LlmSpend.created_at > since,
        )
    )
