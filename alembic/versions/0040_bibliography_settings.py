"""Bibliography settings: citation_style, metadata_waiver columns

Revision ID: 0040
Revises: 0039
Create Date: 2026-06-07

Adds workspace-level citation style preference and per-reference metadata-waiver
flag + reason. See
docs/superpowers/plans/2026-06-07-citation-ledger-slice-4.md (Task 1).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("citation_style", sa.Text(), nullable=False, server_default="vancouver"),
    )
    op.create_check_constraint(
        "ck_workspaces_citation_style",
        "workspaces",
        "citation_style IN ('vancouver','author-year')",
    )
    op.add_column(
        "references",
        sa.Column("metadata_waiver", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "references",
        sa.Column("metadata_waiver_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_citation_style", "workspaces", type_="check")
    op.drop_column("references", "metadata_waiver_reason")
    op.drop_column("references", "metadata_waiver")
    op.drop_column("workspaces", "citation_style")
