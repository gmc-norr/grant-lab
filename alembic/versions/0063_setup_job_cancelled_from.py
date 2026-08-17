"""workspace_setup_jobs: cancelled_from, the state a deletion cancelled from

Revision ID: 0063
Revises: 0062
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_setup_jobs",
        sa.Column("cancelled_from", sa.Text(), nullable=True),
    )
    # Rows cancelled before this migration keep NULL: their source state was
    # never recorded and cannot be recovered. Restore treats them as "not
    # cancelled by a deletion" and leaves them alone, which is the same
    # not-restorable outcome spec 0.3 accepts for pre-Slice-A deletions.
    op.create_check_constraint(
        "ck_workspace_setup_jobs_cancelled_from",
        "workspace_setup_jobs",
        "cancelled_from IS NULL OR cancelled_from IN "
        "('queued','running','waiting_for_rubric_review')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_workspace_setup_jobs_cancelled_from", "workspace_setup_jobs"
    )
    op.drop_column("workspace_setup_jobs", "cancelled_from")
