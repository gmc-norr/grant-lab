"""workspace_setup_jobs: add 'cancelled'; one rubric_initialized event per workspace

Revision ID: 0061
Revises: 0060
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

_OLD_STATUSES = "'queued','running','waiting_for_rubric_review','done','failed'"
_NEW_STATUSES = f"{_OLD_STATUSES},'cancelled'"


def downgrade_statements() -> list[str]:
    """The downgrade as raw SQL, in order, so a test can prove the ordering.

    The conversion MUST precede the constraint: restoring the old CheckConstraint
    while a 'cancelled' row exists fails against any database where a workspace
    has been deleted mid-setup.
    """
    return [
        "DROP INDEX IF EXISTS uq_workspace_events_rubric_initialized",
        "UPDATE workspace_setup_jobs SET status = 'failed' WHERE status = 'cancelled'",
        "ALTER TABLE workspace_setup_jobs DROP CONSTRAINT ck_workspace_setup_jobs_status",
        "ALTER TABLE workspace_setup_jobs ADD CONSTRAINT ck_workspace_setup_jobs_status "
        f"CHECK (status IN ({_OLD_STATUSES}))",
    ]


def upgrade() -> None:
    op.drop_constraint("ck_workspace_setup_jobs_status", "workspace_setup_jobs", type_="check")
    op.create_check_constraint(
        "ck_workspace_setup_jobs_status",
        "workspace_setup_jobs",
        f"status IN ({_NEW_STATUSES})",
    )
    # Backstop for §7 of the spec: the atomic M3 transaction is the mechanism that
    # prevents a duplicate rubric_initialized event; this index is defence in depth.
    op.create_index(
        "uq_workspace_events_rubric_initialized",
        "workspace_events",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'rubric_initialized'"),
    )


def downgrade() -> None:
    for stmt in downgrade_statements():
        op.execute(stmt)
