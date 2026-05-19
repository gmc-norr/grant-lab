"""attachment_jobs

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachment_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["attachments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_attachment_jobs_status",
        ),
        sa.CheckConstraint(
            "kind IN ('describe_figure')",
            name="ck_attachment_jobs_kind",
        ),
    )
    op.create_index(
        "ix_attachment_jobs_status_updated",
        "attachment_jobs",
        ["status", "updated_at"],
    )
    op.create_index(
        "uq_attachment_jobs_active",
        "attachment_jobs",
        ["attachment_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_attachment_jobs_active",
        table_name="attachment_jobs",
    )
    op.drop_index(
        "ix_attachment_jobs_status_updated",
        table_name="attachment_jobs",
    )
    op.drop_table("attachment_jobs")
