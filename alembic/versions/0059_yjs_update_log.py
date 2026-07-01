"""yjs update log

Revision ID: 0059
Revises: 0058

Append-only Yjs CRDT update log for the collaborative editor (Slice 1).
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yjs_updates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("artifact", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("update_bytes", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("artifact IN ('draft','spec')", name="ck_yjs_updates_artifact"),
    )
    op.create_index("ix_yjs_updates_room", "yjs_updates", ["workspace_id", "artifact", "id"])


def downgrade() -> None:
    op.drop_index("ix_yjs_updates_room", table_name="yjs_updates")
    op.drop_table("yjs_updates")
