"""llm_spend — the one table every hosted-LLM call writes to

Revision ID: 0067
Revises: 0066
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_spend",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_out", sa.Integer(), server_default="0", nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("sdk_session_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_llm_spend_workspace_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('chat_turn','compaction','coherence','evaluator','review',"
            "'uncited_claims','citation')",
            name="ck_llm_spend_kind",
        ),
        sa.CheckConstraint("status IN ('ok','failed','timeout')", name="ck_llm_spend_status"),
        sa.CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0", name="ck_llm_spend_tokens_nonneg"
        ),
    )
    op.create_index("ix_llm_spend_created", "llm_spend", ["created_at"])
    op.create_index("ix_llm_spend_ws_created", "llm_spend", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_llm_spend_ws_created", table_name="llm_spend")
    op.drop_index("ix_llm_spend_created", table_name="llm_spend")
    op.drop_table("llm_spend")
