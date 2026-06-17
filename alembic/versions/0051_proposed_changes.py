"""proposed_changes table

Revision ID: 0051
Revises: 0050
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposed_changes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("target_artifact", sa.Text(), nullable=False),
        sa.Column("section_index", sa.Integer(), nullable=False),
        sa.Column("base_commit_sha", sa.Text(), nullable=False),
        sa.Column("base_section_sha", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("origin_kind", sa.Text(), nullable=False),
        sa.Column("origin_session_id", sa.Text(), nullable=True),
        sa.Column("origin_job_id", UUID(as_uuid=True), nullable=True),
        sa.Column("triggering_turn_id", UUID(as_uuid=True), nullable=True),
        sa.Column("triggering_prompt", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("resolved_by", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resulting_commit_sha", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_pc_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["triggering_turn_id"], ["chat_turns.id"], ondelete="SET NULL", name="fk_pc_turn"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL", name="fk_pc_created_by"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"], ["users.id"], ondelete="SET NULL", name="fk_pc_resolved_by"
        ),
    )
    op.create_check_constraint(
        "ck_proposed_changes_status",
        "proposed_changes",
        "status IN ('pending','accepted','rejected','superseded','stale')",
    )
    op.create_check_constraint(
        "ck_proposed_changes_target_artifact",
        "proposed_changes",
        "target_artifact IN ('draft','spec')",
    )
    op.create_check_constraint(
        "ck_proposed_changes_kind",
        "proposed_changes",
        "kind IN ('prose_edit','citation_insert','citation_remove','citation_update')",
    )
    op.create_check_constraint(
        "ck_proposed_changes_origin_kind",
        "proposed_changes",
        "origin_kind IN ('interactive_turn','coherence_session','evaluator_session','human')",
    )
    op.create_index("ix_proposed_changes_ws_status", "proposed_changes", ["workspace_id", "status"])
    op.create_index(
        "ix_proposed_changes_ws_target",
        "proposed_changes",
        ["workspace_id", "target_artifact", "section_index"],
    )


def downgrade() -> None:
    op.drop_table("proposed_changes")
