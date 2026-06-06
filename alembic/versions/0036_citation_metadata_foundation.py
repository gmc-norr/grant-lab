"""Citation metadata foundation: references CSL columns + citation_llm_usage

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-06

Adds CSL-JSON bibliographic metadata + provenance/freezing columns to
references, and a citation_llm_usage ledger for the citation subsystem's LLM
spend (metadata extraction now; audit later). See
docs/superpowers/specs/2026-06-06-citation-provenance-evidence-ledger-design.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("references", sa.Column("csl_json", postgresql.JSONB(), nullable=True))
    op.add_column("references", sa.Column("csl_json_extracted", postgresql.JSONB(), nullable=True))
    op.add_column("references", sa.Column("citekey", sa.Text(), nullable=True))
    op.add_column(
        "references",
        sa.Column("metadata_status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.add_column("references", sa.Column("metadata_source", sa.Text(), nullable=True))
    op.add_column("references", sa.Column("original_sha256", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_references_metadata_status",
        "references",
        "metadata_status IN ('pending','extracted','verified_external','approved','failed')",
    )
    op.create_check_constraint(
        "ck_references_original_sha256_hex",
        "references",
        "original_sha256 IS NULL OR original_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_index(
        "uq_references_workspace_citekey",
        "references",
        ["workspace_id", "citekey"],
        unique=True,
        postgresql_where=sa.text("citekey IS NOT NULL"),
    )

    op.create_table(
        "citation_llm_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('ok','failed','timeout')", name="ck_citation_llm_usage_status"
        ),
        sa.CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0", name="ck_citation_llm_usage_tokens_nonneg"
        ),
    )
    # Composite serves the per-workspace daily sum; standalone created_at
    # serves the GLOBAL daily sum (which filters created_at only — the
    # composite's leading workspace_id column can't serve it efficiently).
    op.create_index(
        "ix_citation_llm_usage_ws_created", "citation_llm_usage", ["workspace_id", "created_at"]
    )
    op.create_index("ix_citation_llm_usage_created", "citation_llm_usage", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_citation_llm_usage_created", table_name="citation_llm_usage")
    op.drop_index("ix_citation_llm_usage_ws_created", table_name="citation_llm_usage")
    op.drop_table("citation_llm_usage")
    op.drop_index("uq_references_workspace_citekey", table_name="references")
    op.drop_constraint("ck_references_original_sha256_hex", "references", type_="check")
    op.drop_constraint("ck_references_metadata_status", "references", type_="check")
    for col in (
        "original_sha256",
        "metadata_source",
        "metadata_status",
        "citekey",
        "csl_json_extracted",
        "csl_json",
    ):
        op.drop_column("references", col)
