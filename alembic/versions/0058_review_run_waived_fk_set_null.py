"""alter review_runs waived_from_review_run_id FK to ON DELETE SET NULL

Revision ID: 0058
Revises: 0057
"""

from __future__ import annotations

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The FK was created without ON DELETE in migration 0026.
    # Postgres auto-named it review_runs_waived_from_review_run_id_fkey.
    # Alter it to SET NULL so deleting a parent run doesn't orphan child rows
    # (or require explicit nullification in application code).
    op.drop_constraint(
        "review_runs_waived_from_review_run_id_fkey",
        "review_runs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "review_runs_waived_from_review_run_id_fkey",
        "review_runs",
        "review_runs",
        ["waived_from_review_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "review_runs_waived_from_review_run_id_fkey",
        "review_runs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "review_runs_waived_from_review_run_id_fkey",
        "review_runs",
        "review_runs",
        ["waived_from_review_run_id"],
        ["id"],
    )
