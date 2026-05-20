"""phase_approvals

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phase_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("approving_user_id", sa.BigInteger(), nullable=False),
        sa.Column("approved_artifact_sha", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["approving_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "phase IN ('spec','plan','draft','review')",
            name="ck_phase_approvals_phase",
        ),
    )
    op.create_index(
        "ix_phase_approvals_ws_phase_at",
        "phase_approvals",
        ["workspace_id", "phase", sa.text("approved_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_phase_approvals_ws_phase_at", table_name="phase_approvals")
    op.drop_table("phase_approvals")
