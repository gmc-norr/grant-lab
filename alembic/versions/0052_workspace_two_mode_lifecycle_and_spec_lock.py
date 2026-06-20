"""two-mode lifecycle + spec lock

Revision ID: 0052
Revises: 0051

current_phase {setup,spec,plan,draft,review} -> {setup,active}; add
spec_locked_at/spec_locked_by to workspaces; create spec_lock_events.

The old CHECK is dropped BEFORE the data UPDATE because 'active' is not a
permitted value under it. Downgrade is LOSSY: the old four-way distinction
among spec/plan/draft/review is unrecoverable (active -> 'draft' best-effort).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the old CHECK first so the UPDATE to 'active' is permitted.
    op.drop_constraint("ck_workspaces_current_phase", "workspaces", type_="check")
    # 2. Collapse the legacy working phases to the single 'active' mode.
    op.execute(
        "UPDATE workspaces SET current_phase='active' "
        "WHERE current_phase IN ('spec','plan','draft','review')"
    )
    # 3. Recreate the CHECK with the two-mode vocabulary.
    op.create_check_constraint(
        "ck_workspaces_current_phase",
        "workspaces",
        "current_phase IN ('setup','active')",
    )
    # 4. Lock state columns (NULL on every existing row => unlocked).
    op.add_column(
        "workspaces",
        sa.Column("spec_locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("workspaces", sa.Column("spec_locked_by", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_workspaces_spec_locked_by",
        "workspaces",
        "users",
        ["spec_locked_by"],
        ["id"],
        ondelete="SET NULL",
    )
    # 5. Audit table.
    op.create_table(
        "spec_lock_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("artifact_commit_sha", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_sle_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], ondelete="SET NULL", name="fk_sle_actor"
        ),
    )
    op.create_check_constraint(
        "ck_spec_lock_events_action",
        "spec_lock_events",
        "action IN ('lock','unlock')",
    )
    op.create_index(
        "ix_spec_lock_events_ws_created",
        "spec_lock_events",
        ["workspace_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    # LOSSY: spec/plan/draft/review were collapsed to 'active' and cannot be
    # recovered; map back to a single representative phase ('draft').
    op.drop_index("ix_spec_lock_events_ws_created", table_name="spec_lock_events")
    op.drop_table("spec_lock_events")
    op.drop_constraint("fk_workspaces_spec_locked_by", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "spec_locked_by")
    op.drop_column("workspaces", "spec_locked_at")
    op.drop_constraint("ck_workspaces_current_phase", "workspaces", type_="check")
    op.execute("UPDATE workspaces SET current_phase='draft' WHERE current_phase='active'")
    op.create_check_constraint(
        "ck_workspaces_current_phase",
        "workspaces",
        "current_phase IN ('setup','spec','plan','draft','review')",
    )
