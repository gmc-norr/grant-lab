"""Reference tombstoning: references.tombstoned_at soft-delete column

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-07

Adds a nullable soft-delete timestamp to references. A cited reference is
tombstoned (this column set) rather than hard-deleted, so existing citations
keep resolving; tombstoned refs are filtered out of discovery and cannot be
newly cited. Mirrors workspaces.soft_deleted_at. See
docs/superpowers/plans/2026-06-06-citation-ledger-slice-2.md (Task 8).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "references",
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("references", "tombstoned_at")
