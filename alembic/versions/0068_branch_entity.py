"""branches — a branch is a row with an immutable id, and the workspace names its root

Revision ID: 0068
Revises: 0067
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "branches",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_sdk_session_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_branches_workspace_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "name", name="uq_branches_ws_name"),
    )
    op.create_index("ix_branches_workspace_id", "branches", ["workspace_id"])

    # The other half of the cycle, added as a separate ALTER — the same shape the
    # model declares with use_alter=True.
    op.add_column("workspaces", sa.Column("root_branch_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_workspaces_root_branch_id",
        "workspaces",
        "branches",
        ["root_branch_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_workspaces_root_branch_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "root_branch_id")
    op.drop_index("ix_branches_workspace_id", table_name="branches")
    op.drop_table("branches")
