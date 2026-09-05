"""workspaces: current_branch, the source of truth for which branch a workspace is on

Revision ID: 0066
Revises: 0065
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so existing rows land somewhere valid before the backfill;
    # NOT NULL so no code path has to consider a null branch.
    op.add_column(
        "workspaces",
        sa.Column(
            "current_branch",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'main'"),
        ),
    )
    # Then correct it from each repo. The default is right for NEW rows only:
    # backfilling every existing row to 'main' is a guess, and a wrong guess
    # makes §4.1's reconciliation check out a branch that may not exist.
    import os

    from sqlalchemy.orm import Session

    from grant_lab.proposal_assist.branch_backfill import backfill_current_branch

    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    with Session(bind=op.get_bind()) as s:
        backfill_current_branch(s, workspaces_dir=workspaces_dir)
        s.commit()


def downgrade() -> None:
    op.drop_column("workspaces", "current_branch")
