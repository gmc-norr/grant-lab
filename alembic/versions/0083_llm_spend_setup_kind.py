"""llm_spend: a `setup` kind for Phase 0's section and rubric extractors

Revision ID: 0083
Revises: 0082

Phase 0 calls Opus twice per new workspace and wrote no ledger row, so neither
the global cap nor the spend view saw it (review 2026-10-06 H2).
"""

from __future__ import annotations

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None

_KINDS = "'chat_turn','compaction','coherence','evaluator','review','uncited_claims','citation'"


def upgrade() -> None:
    op.drop_constraint("ck_llm_spend_kind", "llm_spend", type_="check")
    op.create_check_constraint("ck_llm_spend_kind", "llm_spend", f"kind IN ({_KINDS},'setup')")


def downgrade() -> None:
    # Lossy, documented: the old CHECK cannot hold a `setup` row, so those rows
    # go. They are spend records, and the cap that read them goes with them.
    op.execute("DELETE FROM llm_spend WHERE kind = 'setup'")
    op.drop_constraint("ck_llm_spend_kind", "llm_spend", type_="check")
    op.create_check_constraint("ck_llm_spend_kind", "llm_spend", f"kind IN ({_KINDS})")
