"""Citation audit verdict cache: citation_audit_verdicts

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-07

Adds a semantic-verdict cache for the citation audit validator. Keyed by a
content hash (cache_key) including the relied-on quote, so re-running
validate_citations over an unchanged draft is free; bump validator_version to
invalidate the whole cache. See
docs/superpowers/specs/2026-06-06-citation-provenance-evidence-ledger-design.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "citation_audit_verdicts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("validator_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
            name="fk_citation_audit_verdicts_workspace_id",
        ),
        sa.CheckConstraint(
            "verdict IN ('supports','contradicts','unsupported','relevant','irrelevant')",
            name="ck_citation_audit_verdicts_verdict",
        ),
    )
    # Composite serves cache lookups by (workspace_id, cache_key); standalone
    # created_at serves time-bounded eviction/inspection scans.
    op.create_index(
        "ix_citation_audit_verdicts_ws_cache",
        "citation_audit_verdicts",
        ["workspace_id", "cache_key"],
    )
    op.create_index("ix_citation_audit_verdicts_created", "citation_audit_verdicts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_citation_audit_verdicts_created", table_name="citation_audit_verdicts")
    op.drop_index("ix_citation_audit_verdicts_ws_cache", table_name="citation_audit_verdicts")
    op.drop_table("citation_audit_verdicts")
