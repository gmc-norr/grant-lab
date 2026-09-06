"""freshness fingerprints: record what a vector and a ranking describe

Revision ID: 0075
Revises: 0074
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column("embedding_fingerprint", sa.Text(), nullable=True),
    )
    op.add_column(
        "call_rankings",
        sa.Column("profile_fingerprint", sa.Text(), nullable=True),
    )
    # Grandfather every ranking that predates this migration. 'legacy' means
    # "written before we tracked provenance, assumed current" -- reconcile
    # skips it only while cr.ranked_at >= p.updated_at, so an edit still
    # revokes it. A NULL AFTER this point means a bug and is treated as stale.
    #
    # Deliberately NOT done for profiles.embedding_fingerprint: NULL there
    # means stale, and every profile is re-embedded on the first tick. That
    # costs seconds against a local Ollama for tens of profiles, where a full
    # re-rank would be profile-count x 90 days of calls.
    op.execute("UPDATE call_rankings SET profile_fingerprint = 'legacy'")


def downgrade() -> None:
    op.drop_column("call_rankings", "profile_fingerprint")
    op.drop_column("profiles", "embedding_fingerprint")
