"""call_rubrics template table

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_rubrics",
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("funder_notes", sa.Text(), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extracted_by_model", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("source_text_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("call_id"),
    )


def downgrade() -> None:
    op.drop_table("call_rubrics")
