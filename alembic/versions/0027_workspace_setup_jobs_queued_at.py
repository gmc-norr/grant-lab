"""workspace_setup_jobs: add queued_at for FIFO ordering

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-20
"""

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_setup_jobs",
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspace_setup_jobs", "queued_at")
