"""job tables: claimed_at_generation, the epoch an actor claimed in

Revision ID: 0064
Revises: 0063
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

# The 7 LIVE workspace-scoped job tables. attachment_jobs is deliberately
# EXCLUDED: dead schema with no production reference outside db/models.py and no
# claim path, so it has no belief to carry (spec 4.5).
FENCED_TABLES = (
    "turn_jobs",
    "coherence_jobs",
    "evaluator_jobs",
    "uncited_claims_jobs",
    "workspace_setup_jobs",
    "review_runs",
    "claim_analysis_jobs",
)


def upgrade() -> None:
    for table in FENCED_TABLES:
        op.add_column(
            table,
            # NULLABLE with NO server_default, deliberately. Rows already in
            # flight when this lands have no generation to record, and NULL means
            # "legacy, unfenced" — those rows fall back to the liveness fence and
            # the case disappears as they terminalize.
            #
            # A server_default of 0 would be WRONG, not merely redundant:
            # workspaces.lifecycle_generation itself defaults to 0, so 0 is a
            # REAL generation that a fresh workspace genuinely sits at. Defaulting
            # to 0 would assert "claimed at generation 0" about every legacy row,
            # which fences them against a real epoch instead of exempting them.
            sa.Column("claimed_at_generation", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    for table in reversed(FENCED_TABLES):
        op.drop_column(table, "claimed_at_generation")
