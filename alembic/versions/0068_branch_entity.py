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

    # Then found the branches from each repo. Same split as 0066: the migration is
    # untestable in place, so the half that can be wrong lives in a module with
    # tests. `branches` and `root_branch_id` both exist by this point.
    import os

    from sqlalchemy.orm import Session

    from call_manager.proposal_assist.branch_entity_backfill import backfill_branch_entities

    workspaces_dir = os.environ.get("WORKSPACES_DIR", "var/workspaces")
    with Session(bind=op.get_bind()) as s:
        result = backfill_branch_entities(s, workspaces_dir=workspaces_dir)
        s.commit()
    # Say what happened. A workspace whose repo could not be read is left with no
    # rows and no root pointer, and the migration still succeeds — that is the
    # right trade (one bad repo must not block a deploy) but it must not be
    # silent, or "the migration succeeded" reads as "every workspace was founded".
    print(
        f"0068: founded {result.created} branch rows, removed {result.deleted} orphans, "
        f"set {result.rooted} root pointers, SKIPPED {result.skipped} unreadable repos"
    )


def downgrade() -> None:
    op.drop_constraint("fk_workspaces_root_branch_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "root_branch_id")
    op.drop_index("ix_branches_workspace_id", table_name="branches")
    op.drop_table("branches")
