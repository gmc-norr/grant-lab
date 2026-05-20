"""workspaces: phased-workflow columns

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "current_phase",
            sa.Text(),
            nullable=False,
            server_default="setup",
        ),
    )
    op.create_check_constraint(
        "ck_workspaces_current_phase",
        "workspaces",
        "current_phase IN ('setup','spec','plan','draft','review')",
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "call_body_snapshot",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "profile_desc_snapshot",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "snapshot_refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "rubric_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("rubric_funder_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "rubric_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "section_extracts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("review_runs_per_day_override", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("review_dollar_cap_override", sa.Numeric(6, 3), nullable=True),
    )


def downgrade() -> None:
    for col in (
        "review_dollar_cap_override",
        "review_runs_per_day_override",
        "section_extracts",
        "rubric_updated_at",
        "rubric_funder_notes",
        "rubric_criteria",
        "snapshot_refreshed_at",
        "profile_desc_snapshot",
        "call_body_snapshot",
    ):
        op.drop_column("workspaces", col)
    op.drop_constraint("ck_workspaces_current_phase", "workspaces", type_="check")
    op.drop_column("workspaces", "current_phase")
