"""The LLM spend ledger."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base


class LlmSpend(Base):
    """Append-only ledger of every hosted-LLM call the app makes.

    ONE row per terminal event, written by exactly one function
    (`proposal_assist.spend.record_spend`). The daily caps sum this table and
    nothing else.

    It replaces a re-derivation that summed FIVE tables on THREE different
    timestamp columns (ChatTurn.created_at, CoherenceJob.queued_at,
    EvaluatorJob.queued_at, ReviewRun.triggered_at, UncitedClaimsJob.queued_at).
    Every hosted feature had to independently remember two columns, a write on
    every terminal path, and a SELECT in caps.py — and six sites had already got
    it wrong, all in the same direction: spend that happened and was not
    recorded.

    The per-job `input_tokens`/`output_tokens` columns still exist and are still
    written; the UI reads them. They are a DISPLAY CACHE. This is the source of
    truth, and tests/fencing/spend_guard.py is what keeps the two honest.

    `status` records failures on purpose: a run that errored after consuming
    input tokens cost real money, and the old code recorded zero for it.
    """

    __tablename__ = "llm_spend"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('chat_turn','compaction','coherence','evaluator','review',"
            "'uncited_claims','citation','setup')",
            name="ck_llm_spend_kind",
        ),
        CheckConstraint("status IN ('ok','failed','timeout')", name="ck_llm_spend_status"),
        CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="ck_llm_spend_tokens_nonneg"),
        # The two reads the caps make: whole-instance by day, and per-workspace
        # by day. Both filter on created_at first.
        Index("ix_llm_spend_created", "created_at"),
        Index("ix_llm_spend_ws_created", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE", name="fk_llm_spend_workspace_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # The job/turn row this spend belongs to, as text so one column serves both
    # uuid and int primary keys. Nullable: compaction has no job row.
    ref_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    sdk_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
