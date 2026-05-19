"""call_section_extracts template table

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_section_extracts",
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("extract_text", sa.Text(), nullable=False),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("extracted_by_model", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("source_text_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("call_id", "section"),
        sa.CheckConstraint(
            "section IN ('eligibility','scope','budget',"
            "'expected_outcomes','restrictions','mandatory')",
            name="ck_call_section_extracts_section",
        ),
    )


def downgrade() -> None:
    op.drop_table("call_section_extracts")
