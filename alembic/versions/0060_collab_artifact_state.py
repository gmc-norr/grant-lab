"""collab artifact state (flag + checkpoint watermark)

Revision ID: 0060
Revises: 0059
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collab_artifact_state",
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("artifact", sa.Text(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checkpoint_update_id", sa.BigInteger(), nullable=True),
        sa.Column("last_checkpoint_commit_sha", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "artifact IN ('draft','spec')", name="ck_collab_artifact_state_artifact"
        ),
        sa.PrimaryKeyConstraint("workspace_id", "artifact"),
    )


def downgrade() -> None:
    op.drop_table("collab_artifact_state")
